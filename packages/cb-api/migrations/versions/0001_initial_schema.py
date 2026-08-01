"""initial schema — Citus-shaped, single-node

Mongo's 12 collections become relational tables sharded on `group_id`, the natural
tenant key. Colocation means a group's config + members + admins + events all live
on the same shard, so every per-group join is node-local.

Reference tables (users, blacklist, bots, command_catalog) are replicated to every
node because they are small and joined from everywhere.

Indexes exist here that never existed in Mongo (FEATURE-MAP D10): the birthday
lookup was an un-indexable `$expr` full collection scan; here it is a stored
generated column with a composite index.

Revision ID: 0001
Revises:
"""

from __future__ import annotations

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------ extensions
    op.execute("CREATE EXTENSION IF NOT EXISTS citus")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")

    # Single-node Citus: the coordinator must register itself before it can hold
    # shards. Adding real workers later needs no schema change.
    #
    # Two steps, not one. `citus_set_coordinator_host` registers the coordinator
    # with shouldhaveshards = false, which is right for a real cluster and fatal
    # for one node: with no worker registered, the first
    # `create_distributed_table` fails with
    #   replication_factor (1) exceeds number of worker nodes (0)
    # So when this is the only node, mark it shard-holding as well. Once real
    # workers exist the placement decision is theirs and this leaves it alone.
    #
    # The host comes from `citus.local_hostname` (default `localhost`) rather than
    # a hardcoded compose service name: it is the address nodes use to reach the
    # coordinator, so it has to be resolvable from inside the database, not from
    # wherever alembic happens to run.
    op.execute(
        """
        DO $$
        DECLARE
            host    text := coalesce(nullif(current_setting('citus.local_hostname', true), ''),
                                     'localhost');
            pgport  int  := current_setting('port')::int;
            workers int;
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_dist_node WHERE groupid = 0) THEN
                PERFORM citus_set_coordinator_host(host, pgport);
            END IF;

            SELECT count(*) INTO workers FROM pg_dist_node WHERE groupid <> 0 AND isactive;
            IF workers = 0 THEN
                PERFORM citus_set_node_property(nodename, nodeport, 'shouldhaveshards', true)
                FROM pg_dist_node WHERE groupid = 0;
            END IF;
        END $$;
        """
    )

    # ------------------------------------------------------- reference tables (global)
    op.execute(
        """
        CREATE TABLE users (
            user_id        bigint PRIMARY KEY,
            username       text,
            first_name     text,
            last_name      text,
            language_code  text,
            birthdate      date,
            -- v1 stored birthdate then matched it with an $expr month/day pipeline,
            -- which no index can serve. Stored generated columns fix that.
            birth_month    smallint GENERATED ALWAYS AS (EXTRACT(MONTH FROM birthdate)::smallint) STORED,
            birth_day      smallint GENERATED ALWAYS AS (EXTRACT(DAY   FROM birthdate)::smallint) STORED,
            is_bot         boolean NOT NULL DEFAULT false,
            created_at     timestamptz NOT NULL DEFAULT now(),
            updated_at     timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX users_username_idx ON users (lower(username)) WHERE username IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX users_birthday_idx ON users (birth_month, birth_day) "
        "WHERE birthdate IS NOT NULL"
    )

    op.execute(
        """
        CREATE TABLE blacklist (
            subject_id  bigint PRIMARY KEY,          -- user_id or chat_id
            kind        text NOT NULL DEFAULT 'user' CHECK (kind IN ('user', 'chat')),
            reason      text,
            source      text NOT NULL DEFAULT 'manual',  -- manual | cas | doomlist
            created_at  timestamptz NOT NULL DEFAULT now()
        )
        """
    )

    op.execute(
        """
        CREATE TABLE bots (
            bot_id     int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            skin       text NOT NULL UNIQUE,   -- cookiebot | bombot | pawsy | tarinbot
            username   text NOT NULL UNIQUE,
            display_name text NOT NULL,
            active     boolean NOT NULL DEFAULT true,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )

    op.execute(
        """
        CREATE TABLE command_catalog (
            command     text PRIMARY KEY,
            category    text NOT NULL CHECK (category IN ('core', 'fun', 'util', 'owner')),
            admin_only  boolean NOT NULL DEFAULT false,
            feature_flag text,                  -- functions_fun | functions_utility | NULL
            qa_feature  text,                   -- Cookiebot-QA feature file, for traceability
            enabled     boolean NOT NULL DEFAULT true
        )
        """
    )

    for table in ("users", "blacklist", "bots", "command_catalog"):
        op.execute(f"SELECT create_reference_table('{table}')")

    # -------------------------------------------- distributed tables (shard: group_id)
    op.execute(
        """
        CREATE TABLE groups (
            group_id    bigint NOT NULL,
            title       text,
            username    text,
            image_url   text,
            chat_type   text NOT NULL DEFAULT 'supergroup',
            skin        text NOT NULL DEFAULT 'cookiebot',
            joined_at   timestamptz NOT NULL DEFAULT now(),
            left_at     timestamptz,
            PRIMARY KEY (group_id)
        )
        """
    )

    # Column defaults are v1's defaults, transcribed from the one place they exist:
    # `Configurations.py:111`, the tuple assigned before the backend is consulted.
    # (The Java `Config.java` has none — every field is a nullable Lombok property,
    # so a group the backend has never seen is served that Python tuple.) Getting
    # these wrong is a silent regression: they are what a group created on v2 gets
    # forever, and nobody notices until captcha or media restriction behaves
    # differently from the same group on v1.
    #
    #   limbotimespan  = 600  seconds — `round(x/60)` minutes in the restrict text
    #   captchatimespan = 300 seconds — `round(x/60)` minutes in the captcha caption
    #   maxPosts       = 9999         — v1's "unlimited"
    #   threadPosts    = "9999"       — the same sentinel as a string; NULL here
    #
    # Two deliberate divergences:
    #   language          v1 defaults to 'pt'. v1 in practice overwrites it from the
    #                     joining user's Telegram language_code (COOKIEBOT.py:133-134),
    #                     which v2 does too, so this only decides what a group with no
    #                     language signal gets. 'en' is the neutral answer for a
    #                     multi-tenant deployment; see docs/site/content/docs/feature-map.mdx.
    #   sticker_spam_window_s  v1 has no window at all — its counter never resets
    #                     (Cooldowns.py), so a group accumulates strikes forever.
    #                     That is a defect, not behaviour worth preserving.
    op.execute(
        """
        CREATE TABLE group_configs (
            group_id                 bigint NOT NULL,
            allow_furbots            boolean NOT NULL DEFAULT true,
            sticker_spam_limit       int     NOT NULL DEFAULT 5,
            sticker_spam_window_s    int     NOT NULL DEFAULT 60,
            media_restrict_seconds   int     NOT NULL DEFAULT 600,
            captcha_timeout_seconds  int     NOT NULL DEFAULT 300,
            functions_fun            boolean NOT NULL DEFAULT true,
            functions_utility        boolean NOT NULL DEFAULT true,
            sfw                      boolean NOT NULL DEFAULT true,
            language                 text    NOT NULL DEFAULT 'en',
            publisher_post           boolean NOT NULL DEFAULT false,
            publisher_ask            boolean NOT NULL DEFAULT true,
            publisher_members_only   boolean NOT NULL DEFAULT false,
            thread_posts             text,
            max_posts                int     NOT NULL DEFAULT 9999,
            doomlist_enabled         boolean NOT NULL DEFAULT true,
            updated_at               timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (group_id),
            FOREIGN KEY (group_id) REFERENCES groups (group_id) ON DELETE CASCADE
        )
        """
    )

    op.execute(
        """
        CREATE TABLE group_rules (
            group_id   bigint NOT NULL,
            body       text NOT NULL,
            updated_by bigint,
            updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (group_id),
            FOREIGN KEY (group_id) REFERENCES groups (group_id) ON DELETE CASCADE
        )
        """
    )

    op.execute(
        """
        CREATE TABLE group_welcomes (
            group_id   bigint NOT NULL,
            body       text NOT NULL,
            updated_by bigint,
            updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (group_id),
            FOREIGN KEY (group_id) REFERENCES groups (group_id) ON DELETE CASCADE
        )
        """
    )

    op.execute(
        """
        CREATE TABLE group_members (
            group_id      bigint NOT NULL,
            user_id       bigint NOT NULL,
            joined_at     timestamptz NOT NULL DEFAULT now(),
            left_at       timestamptz,
            first_seen_at timestamptz NOT NULL DEFAULT now(),
            message_count bigint NOT NULL DEFAULT 0,
            PRIMARY KEY (group_id, user_id),
            FOREIGN KEY (group_id) REFERENCES groups (group_id) ON DELETE CASCADE
        )
        """
    )
    # core_mediarestrict: "has this member been here longer than the limit?"
    op.execute(
        "CREATE INDEX group_members_joined_idx ON group_members (group_id, joined_at) "
        "WHERE left_at IS NULL"
    )

    op.execute(
        """
        CREATE TABLE group_admins (
            group_id   bigint NOT NULL,
            user_id    bigint NOT NULL,
            role       text NOT NULL DEFAULT 'administrator'
                       CHECK (role IN ('creator', 'administrator')),
            anonymous  boolean NOT NULL DEFAULT false,
            synced_at  timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (group_id, user_id),
            FOREIGN KEY (group_id) REFERENCES groups (group_id) ON DELETE CASCADE
        )
        """
    )

    op.execute(
        """
        CREATE TABLE captcha_challenges (
            group_id    bigint NOT NULL,
            user_id     bigint NOT NULL,
            nonce       text   NOT NULL,
            kind        text   NOT NULL,
            answer      text   NOT NULL,
            attempts    int    NOT NULL DEFAULT 0,
            message_id  bigint,
            issued_at   timestamptz NOT NULL DEFAULT now(),
            expires_at  timestamptz NOT NULL,
            solved_at   timestamptz,
            PRIMARY KEY (group_id, user_id),
            FOREIGN KEY (group_id) REFERENCES groups (group_id) ON DELETE CASCADE
        )
        """
    )
    # The expiry sweep is a worker cron job, not a per-message file rewrite.
    op.execute(
        "CREATE INDEX captcha_expiry_idx ON captcha_challenges (expires_at) WHERE solved_at IS NULL"
    )

    for table in (
        "groups",
        "group_configs",
        "group_rules",
        "group_welcomes",
        "group_members",
        "group_admins",
        "captcha_challenges",
    ):
        colocate = "" if table == "groups" else ", colocate_with => 'groups'"
        op.execute(f"SELECT create_distributed_table('{table}', 'group_id'{colocate})")

    # ------------------------------------------------------------- analytics fact table
    op.execute(
        """
        CREATE TABLE message_events (
            ts           timestamptz NOT NULL,
            group_id     bigint      NOT NULL,
            user_id      bigint,
            bot_id       int,
            event_type   text        NOT NULL,   -- message|command|join|leave|callback|captcha|moderation
            command      text,
            outcome      text        NOT NULL DEFAULT 'ok',
            latency_ms   int,
            handler      text,
            media_kind   text,
            llm_tokens   int,
            llm_cost_usd numeric(12, 6),
            trace_id     text,                   -- joins a dashboard row to its Tempo trace
            attrs        jsonb
        ) PARTITION BY RANGE (ts)
        """
    )
    op.execute(
        "SELECT create_distributed_table('message_events', 'group_id', colocate_with => 'groups')"
    )

    # ------------------------------------------------------------------ rollup tables
    # Dashboards read these, never the raw fact table.
    op.execute(
        """
        CREATE TABLE group_daily_stats (
            group_id        bigint NOT NULL,
            day             date   NOT NULL,
            messages        bigint NOT NULL DEFAULT 0,
            commands        bigint NOT NULL DEFAULT 0,
            joins           bigint NOT NULL DEFAULT 0,
            leaves          bigint NOT NULL DEFAULT 0,
            captcha_issued  bigint NOT NULL DEFAULT 0,
            captcha_solved  bigint NOT NULL DEFAULT 0,
            active_users    bigint NOT NULL DEFAULT 0,
            errors          bigint NOT NULL DEFAULT 0,
            p95_latency_ms  int,
            llm_tokens      bigint NOT NULL DEFAULT 0,
            llm_cost_usd    numeric(12, 4) NOT NULL DEFAULT 0,
            computed_at     timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (group_id, day)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE command_daily_stats (
            group_id       bigint NOT NULL,
            day            date   NOT NULL,
            command        text   NOT NULL,
            invocations    bigint NOT NULL DEFAULT 0,
            errors         bigint NOT NULL DEFAULT 0,
            p95_latency_ms int,
            PRIMARY KEY (group_id, day, command)
        )
        """
    )
    for table in ("group_daily_stats", "command_daily_stats"):
        op.execute(
            f"SELECT create_distributed_table('{table}', 'group_id', colocate_with => 'groups')"
        )

    # --------------------------------------------------------- partition maintenance
    # pg_partman is not in the citus image, and pg_cron is not guaranteed either,
    # so partition creation and columnar conversion are a plain SQL function that
    # cb-worker calls on a cron schedule (see cb_worker/main.py).
    op.execute(
        """
        CREATE OR REPLACE FUNCTION cb_maintain_partitions(
            days_ahead int DEFAULT 7,
            columnar_after_days int DEFAULT 7
        ) RETURNS int AS $$
        DECLARE
            d date;
            part_name text;
            created int := 0;
        BEGIN
            -- create tomorrow..+days_ahead
            FOR d IN
                SELECT generate_series(current_date, current_date + days_ahead, '1 day')::date
            LOOP
                part_name := format('message_events_p%s', to_char(d, 'YYYY_MM_DD'));
                IF to_regclass(part_name) IS NULL THEN
                    EXECUTE format(
                        'CREATE TABLE %I PARTITION OF message_events FOR VALUES FROM (%L) TO (%L)',
                        part_name, d, d + 1
                    );
                    created := created + 1;
                END IF;
            END LOOP;

            -- compress partitions older than columnar_after_days (~5-10x, fast scans)
            FOR part_name IN
                SELECT c.relname
                FROM pg_class c
                JOIN pg_inherits i ON i.inhrelid = c.oid
                WHERE i.inhparent = 'message_events'::regclass
                  AND c.relname ~ '^message_events_p\\d{4}_\\d{2}_\\d{2}$'
                  AND to_date(right(c.relname, 10), 'YYYY_MM_DD')
                      < current_date - columnar_after_days
            LOOP
                BEGIN
                    PERFORM alter_table_set_access_method(part_name, 'columnar');
                EXCEPTION WHEN OTHERS THEN
                    RAISE NOTICE 'columnar conversion skipped for %: %', part_name, SQLERRM;
                END;
            END LOOP;

            RETURN created;
        END $$ LANGUAGE plpgsql;
        """
    )
    op.execute("SELECT cb_maintain_partitions(7, 7)")

    # ------------------------------------------------------------------ rollup routine
    op.execute(
        """
        CREATE OR REPLACE FUNCTION cb_rollup_day(target date) RETURNS void AS $$
        BEGIN
            INSERT INTO group_daily_stats AS g (
                group_id, day, messages, commands, joins, leaves,
                captcha_issued, captcha_solved, active_users, errors,
                p95_latency_ms, llm_tokens, llm_cost_usd, computed_at
            )
            SELECT
                group_id,
                target,
                count(*) FILTER (WHERE event_type = 'message'),
                count(*) FILTER (WHERE event_type = 'command'),
                count(*) FILTER (WHERE event_type = 'join'),
                count(*) FILTER (WHERE event_type = 'leave'),
                count(*) FILTER (WHERE event_type = 'captcha' AND outcome = 'issued'),
                count(*) FILTER (WHERE event_type = 'captcha' AND outcome = 'solved'),
                count(DISTINCT user_id),
                count(*) FILTER (WHERE outcome = 'error'),
                percentile_disc(0.95) WITHIN GROUP (ORDER BY latency_ms),
                coalesce(sum(llm_tokens), 0),
                coalesce(sum(llm_cost_usd), 0),
                now()
            FROM message_events
            WHERE ts >= target AND ts < target + 1
            GROUP BY group_id
            ON CONFLICT (group_id, day) DO UPDATE SET
                messages = excluded.messages,
                commands = excluded.commands,
                joins = excluded.joins,
                leaves = excluded.leaves,
                captcha_issued = excluded.captcha_issued,
                captcha_solved = excluded.captcha_solved,
                active_users = excluded.active_users,
                errors = excluded.errors,
                p95_latency_ms = excluded.p95_latency_ms,
                llm_tokens = excluded.llm_tokens,
                llm_cost_usd = excluded.llm_cost_usd,
                -- excluded.computed_at, not now(): Citus rejects non-IMMUTABLE
                -- functions in the DO UPDATE SET of an insert on a distributed
                -- table, because each shard would evaluate its own. The value
                -- comes from the now() in the SELECT list above, evaluated once.
                computed_at = excluded.computed_at;

            INSERT INTO command_daily_stats AS c (
                group_id, day, command, invocations, errors, p95_latency_ms
            )
            SELECT
                group_id, target, command,
                count(*),
                count(*) FILTER (WHERE outcome = 'error'),
                percentile_disc(0.95) WITHIN GROUP (ORDER BY latency_ms)
            FROM message_events
            WHERE ts >= target AND ts < target + 1 AND command IS NOT NULL
            GROUP BY group_id, command
            ON CONFLICT (group_id, day, command) DO UPDATE SET
                invocations = excluded.invocations,
                errors = excluded.errors,
                p95_latency_ms = excluded.p95_latency_ms;
        END $$ LANGUAGE plpgsql;
        """
    )

    # ------------------------------------------------------------------- seed catalog
    op.execute(
        """
        INSERT INTO command_catalog (command, category, admin_only, feature_flag, qa_feature) VALUES
            ('commands',      'core', false, NULL,                'core_listcommand'),
            ('privacy',       'core', false, NULL,                'core_privacy'),
            ('rules',         'core', false, NULL,                'core_rules'),
            ('newrules',      'core', true,  NULL,                'core_rules'),
            ('newwelcome',    'core', true,  NULL,                'core_welcome'),
            ('config',        'core', true,  NULL,                'util_config'),
            ('isalive',       'util', false, NULL,                'util_isalive'),
            ('dice',          'fun',  false, 'functions_utility', 'fun_dice'),
            ('ship',          'fun',  false, 'functions_fun',     'fun_ship'),
            ('death',         'fun',  false, 'functions_fun',     'fun_death'),
            ('meme',          'fun',  false, 'functions_fun',     'fun_meme'),
            ('battle',        'fun',  false, 'functions_fun',     'fun_battle'),
            ('random',        'fun',  false, 'functions_fun',     'fun_random'),
            ('firecracker',   'fun',  false, 'functions_fun',     'fun_firecracker'),
            ('complaint',     'fun',  false, 'functions_fun',     'fun_complaint'),
            ('con_bff',       'fun',  false, NULL,                'fun_partneredcons'),
            ('con_patas',     'fun',  false, NULL,                'fun_partneredcons'),
            ('con_fursmeet',  'fun',  false, NULL,                'fun_partneredcons'),
            ('con_trex',      'fun',  false, NULL,                'fun_partneredcons'),
            ('con_furcamp',   'fun',  false, NULL,                'fun_partneredcons'),
            ('con_pawstral',  'fun',  false, NULL,                'fun_partneredcons'),
            ('birthday',      'util', false, 'functions_fun',     'util_birthday'),
            ('nextbirthday',  'util', false, 'functions_fun',     'util_nextbirthday'),
            ('everyone',      'util', true,  NULL,                'util_everyone'),
            ('calladms',      'util', false, NULL,                'util_calladms'),
            ('youtube',       'util', false, 'functions_utility', 'util_youtube'),
            ('deletereposts', 'util', true,  NULL,                'util_deletereposts'),
            ('publish',       'util', false, NULL,                'util_postforwarder'),
            ('repost',        'util', true,  NULL,                'util_postforwarder')
        """
    )
    op.execute(
        """
        INSERT INTO bots (skin, username, display_name) VALUES
            ('cookiebot', 'CookieMWbot',  'Cookiebot'),
            ('bombot',    'MekhysBombot', 'Bombot')
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS cb_rollup_day(date)")
    op.execute("DROP FUNCTION IF EXISTS cb_maintain_partitions(int, int)")
    for table in (
        "command_daily_stats",
        "group_daily_stats",
        "message_events",
        "captcha_challenges",
        "group_admins",
        "group_members",
        "group_welcomes",
        "group_rules",
        "group_configs",
        "groups",
        "command_catalog",
        "bots",
        "blacklist",
        "users",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
