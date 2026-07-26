"""media objects, blob registry, llm usage accounting

Citus notes — the shape here is chosen to keep block exchange out of the hot path:

* `media_objects` and `llm_usage` are distributed on `group_id` and **colocated
  with `groups`**, so every read and write carries the distribution column and is
  a single-shard router query. No shard-to-shard traffic, no coordinator fan-out.
* `media_blobs` is a **reference table** — replicated to every node. A blob row is
  ~200 bytes and there is one per *unique* blob (not per group reference), so
  replication is cheap, and it makes `media_objects ⋈ media_blobs` a node-local
  join instead of a repartition join. If that table ever outgrows replication,
  distribute it on `content_hash` and move the join into the scheduled GC job,
  which can afford the exchange; nothing on the reply path would change.
* Every unique constraint on a distributed table includes `group_id`, which Citus
  requires and which also expresses the intent: dedupe is per tenant.

Identifiers are UUIDv7 generated in the application (`cb_core.ids.uuid7`) so
inserts append to the right edge of the index and no coordinator sequence is
involved. `cb_uuid_v7()` exists for the rare server-side default; Postgres 18's
native `uuidv7()` can replace it once Citus ships on 18.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---------------------------------------------------------------- uuid v7
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")  # gen_random_bytes
    # Postgres 17 has no native uuidv7(). This mirrors RFC 9562: 48-bit big-endian
    # millisecond timestamp, version 7, variant 0b10, random elsewhere.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION cb_uuid_v7() RETURNS uuid AS $$
        DECLARE
            unix_ts_ms bytea;
            uuid_bytes bytea;
        BEGIN
            unix_ts_ms := substring(
                int8send((extract(epoch FROM clock_timestamp()) * 1000)::bigint) from 3
            );
            uuid_bytes := unix_ts_ms || gen_random_bytes(10);
            -- version 7
            uuid_bytes := set_byte(uuid_bytes, 6, (b'0111' || get_byte(uuid_bytes, 6)::bit(4))::bit(8)::int);
            -- variant 0b10
            uuid_bytes := set_byte(uuid_bytes, 8, (b'10' || get_byte(uuid_bytes, 8)::bit(6))::bit(8)::int);
            RETURN encode(uuid_bytes, 'hex')::uuid;
        END $$ LANGUAGE plpgsql VOLATILE;
        """
    )

    # ------------------------------------------------- blob registry (reference)
    op.execute(
        """
        CREATE TABLE media_blobs (
            content_hash text PRIMARY KEY,          -- blake3, 128-bit hex
            blob_key     text NOT NULL UNIQUE,      -- key in the object store
            kind         text NOT NULL,
            byte_size    bigint NOT NULL,
            content_type text,
            backend      text NOT NULL,             -- s3 | gs | file | memory
            created_at   timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("SELECT create_reference_table('media_blobs')")

    # --------------------------------------- per-group references (distributed)
    op.execute(
        """
        CREATE TABLE media_objects (
            group_id         bigint NOT NULL,
            media_id         uuid   NOT NULL,       -- UUIDv7, app-generated
            kind             text   NOT NULL,
            content_hash     text   NOT NULL,
            blob_key         text   NOT NULL,
            byte_size        bigint NOT NULL,
            content_type     text,
            telegram_file_id text,
            uploaded_by      bigint,
            sfw              boolean NOT NULL DEFAULT true,
            created_at       timestamptz NOT NULL DEFAULT now(),
            last_seen_at     timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (group_id, media_id),
            -- dedupe is per tenant; group_id is required in every unique index
            -- on a distributed table.
            UNIQUE (group_id, content_hash),
            FOREIGN KEY (group_id) REFERENCES groups (group_id) ON DELETE CASCADE
        )
        """
    )
    # Serves `/random` (fun_random.feature): group_id first so the planner routes
    # to one shard, then kind, then the sfw filter.
    op.execute(
        "CREATE INDEX media_objects_pick_idx ON media_objects (group_id, kind, sfw) "
        "INCLUDE (blob_key)"
    )
    op.execute("CREATE INDEX media_objects_recent_idx ON media_objects (group_id, media_id DESC)")
    op.execute(
        "SELECT create_distributed_table('media_objects', 'group_id', colocate_with => 'groups')"
    )

    # -------------------------------------------------- llm accounting (distributed)
    op.execute(
        """
        CREATE TABLE llm_usage (
            group_id          bigint NOT NULL,
            usage_id          uuid   NOT NULL,      -- UUIDv7
            user_id           bigint,
            task              text   NOT NULL,      -- chat | moderate | summarize | vision | transcribe
            provider          text   NOT NULL,
            model             text   NOT NULL,
            input_tokens      int    NOT NULL DEFAULT 0,
            output_tokens     int    NOT NULL DEFAULT 0,
            cache_read_tokens int    NOT NULL DEFAULT 0,
            cost_usd          numeric(12, 6),       -- NULL when the model has no known price
            latency_ms        int,
            outcome           text   NOT NULL DEFAULT 'ok',
            trace_id          text,
            created_at        timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (group_id, usage_id),
            FOREIGN KEY (group_id) REFERENCES groups (group_id) ON DELETE CASCADE
        )
        """
    )
    op.execute("CREATE INDEX llm_usage_time_idx ON llm_usage (group_id, created_at DESC)")
    op.execute("CREATE INDEX llm_usage_model_idx ON llm_usage (group_id, model, created_at DESC)")
    op.execute(
        "SELECT create_distributed_table('llm_usage', 'group_id', colocate_with => 'groups')"
    )

    op.execute(
        """
        CREATE TABLE llm_daily_cost (
            group_id      bigint NOT NULL,
            day           date   NOT NULL,
            provider      text   NOT NULL,
            model         text   NOT NULL,
            calls         bigint NOT NULL DEFAULT 0,
            input_tokens  bigint NOT NULL DEFAULT 0,
            output_tokens bigint NOT NULL DEFAULT 0,
            cost_usd      numeric(12, 4) NOT NULL DEFAULT 0,
            refusals      bigint NOT NULL DEFAULT 0,
            errors        bigint NOT NULL DEFAULT 0,
            computed_at   timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (group_id, day, provider, model)
        )
        """
    )
    op.execute(
        "SELECT create_distributed_table('llm_daily_cost', 'group_id', colocate_with => 'groups')"
    )

    # Colocated rollup: the GROUP BY carries group_id, so each shard aggregates
    # locally and only the (already tiny) result crosses the network.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION cb_rollup_llm_day(target date) RETURNS void AS $$
        BEGIN
            INSERT INTO llm_daily_cost AS d (
                group_id, day, provider, model, calls,
                input_tokens, output_tokens, cost_usd, refusals, errors, computed_at
            )
            SELECT
                group_id, target, provider, model,
                count(*),
                coalesce(sum(input_tokens), 0),
                coalesce(sum(output_tokens), 0),
                coalesce(sum(cost_usd), 0),
                count(*) FILTER (WHERE outcome = 'refusal'),
                count(*) FILTER (WHERE outcome = 'error'),
                now()
            FROM llm_usage
            WHERE created_at >= target AND created_at < target + 1
            GROUP BY group_id, provider, model
            ON CONFLICT (group_id, day, provider, model) DO UPDATE SET
                calls = excluded.calls,
                input_tokens = excluded.input_tokens,
                output_tokens = excluded.output_tokens,
                cost_usd = excluded.cost_usd,
                refusals = excluded.refusals,
                errors = excluded.errors,
                -- Citus rejects non-IMMUTABLE functions in DO UPDATE SET on a
                -- distributed table; the now() in the SELECT list above is the
                -- one timestamp for the whole statement. Same as cb_rollup_day.
                computed_at = excluded.computed_at;
        END $$ LANGUAGE plpgsql;
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS cb_rollup_llm_day(date)")
    for table in ("llm_daily_cost", "llm_usage", "media_objects", "media_blobs"):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    op.execute("DROP FUNCTION IF EXISTS cb_uuid_v7()")
