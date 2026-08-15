"""x_miniapp_auth's refresh tokens, and x_audit_log's ledger

Two tables, two different shapes, for the same reason `sticker_pool` is a
reference table and `media_objects` is distributed: what the row is *about*
decides where it lives.

## `refresh_tokens` — reference

A refresh token belongs to a Telegram **user**, not to a group: one Mini App
session lists every group that user administers, and the session outlives any
one of them. There is no `group_id` to shard on, the table is read once per
refresh (minutes apart, not per request), and it stays small — one live row per
session, and expired rows are deleted. That is `users`/`blacklist`/`bots`/
`signing_keys` again (`0001_initial_schema.py:136`, `0008_signing_keys.py`).

**Only a hash is stored.** `token_hash` is SHA-256 of the opaque token; the
token itself is returned to the client once and never persisted, so a database
copy cannot be replayed as a session. `family_id` groups a token with every
token it was rotated from: presenting an already-rotated token means either a
client bug or a stolen copy, and the response to both is to kill the family
rather than guess which one it was.

## `group_audit_events` — distributed on `group_id`

An audit row is a fact about one group, read by that group's admins, written on
every admin action. `group_id` is the shard key and leads the primary key, so
"this group's last 50 actions" is a single-shard router query with no fan-out
(AGENTS.md §4) — verified in `qa/integration/test_citus_topology.py`.

`id` is a UUIDv7 from `cb_core.ids.uuid7`, so `ORDER BY id DESC` is
chronological and keyset pagination needs no second index (`cb_core/ids.py`).
`before`/`after` are jsonb holding only the fields that changed — an audit row
is evidence, and evidence that paraphrases is worth less than evidence that
quotes.

Retention is deliberately not a `DELETE` job here: `message_events` sheds
partitions because it is high-volume telemetry, while an audit trail that
quietly forgets is the one kind of log nobody wants. If a deployment needs a
retention window it is a policy decision, made with a migration of its own.

Revision ID: 0010
Revises: 0009
"""

from __future__ import annotations

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE refresh_tokens (
            token_hash  text PRIMARY KEY,
            family_id   uuid        NOT NULL,
            user_id     bigint      NOT NULL,
            scope       text        NOT NULL DEFAULT '',
            audience    text        NOT NULL DEFAULT '',
            issued_at   timestamptz NOT NULL DEFAULT now(),
            expires_at  timestamptz NOT NULL,
            used_at     timestamptz,
            revoked_at  timestamptz
        )
        """
    )
    op.execute("SELECT create_reference_table('refresh_tokens')")
    # Refresh revokes a whole family at once, and the expiry sweep deletes by
    # time; both are the only two ways this table is read besides the PK.
    op.execute("CREATE INDEX refresh_tokens_family_idx ON refresh_tokens (family_id)")
    op.execute("CREATE INDEX refresh_tokens_expiry_idx ON refresh_tokens (expires_at)")

    op.execute(
        """
        CREATE TABLE group_audit_events (
            group_id      bigint      NOT NULL,
            id            uuid        NOT NULL,
            ts            timestamptz NOT NULL DEFAULT now(),
            actor_user_id bigint,
            actor_kind    text        NOT NULL DEFAULT 'admin',
            action        text        NOT NULL,
            surface       text        NOT NULL DEFAULT 'api',
            summary       text,
            before        jsonb,
            after         jsonb,
            trace_id      text,
            PRIMARY KEY (group_id, id)
        )
        """
    )
    op.execute(
        "SELECT create_distributed_table('group_audit_events', 'group_id', "
        "colocate_with => 'groups')"
    )
    # The one filtered read the API offers besides the plain page: "what did
    # this actor do", and "show me only config changes". Both lead with the
    # shard key so they stay router queries.
    op.execute(
        "CREATE INDEX group_audit_events_action_idx "
        "ON group_audit_events (group_id, action, id DESC)"
    )
    op.execute(
        "CREATE INDEX group_audit_events_actor_idx "
        "ON group_audit_events (group_id, actor_user_id, id DESC)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS group_audit_events")
    op.execute("DROP TABLE IF EXISTS refresh_tokens")
