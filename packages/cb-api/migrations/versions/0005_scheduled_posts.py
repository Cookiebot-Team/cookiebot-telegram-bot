"""scheduled_posts: v1's publisher queue, out of a local SQLite file

v1 kept every scheduled post in `Publisher.db`
(`../COOKIEBOT-Telegram-Group-Bot/Bot/Publisher.py:15-17`), a SQLite file opened
once at import with `check_same_thread=False`, no lock, and shared by every
worker thread — FEATURE-MAP's D5. It also lived on one host, so the schedule was
invisible to any other process and unrecoverable if that host was replaced.

Distributed on `group_id`, colocated with `groups`. `group_id` is the **target**
group — the one that receives the forward — because that is what the two hot
readers filter on: the delivery cron's per-group sweep and the receiving group's
own consent check.

## What replaces v1's `name`

v1 had no primary key. A row's identity was `name`, a formatted string
(`f"{origin_title} --> {target_title}, at {hour}:{minute}"`), which was then
parsed three separate ways to answer three different questions:

  * `name.split('-->')[0].strip()`      -> which source channel is this?     (:240)
  * `f"--> {target_title}" in name`     -> which group does it target?       (:262)
  * `name.startswith(button_text)`      -> which post was this a reply to?   (:361)

All three are substring scans over every row, and a group actually titled
`A --> B` collides with all of them. Here they are three columns —
`origin_title`, `target_title`, and a `uuid7` surrogate key (AGENTS.md §2.3) —
so each question is an indexed predicate. `origin_title` keeps v1's exact value
so the reply relay still matches the inline-keyboard button that carries it.

Revision ID: 0005
Revises: 0004
"""

from __future__ import annotations

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE scheduled_posts (
            group_id             bigint      NOT NULL,
            post_id              uuid        NOT NULL,
            origin_title         text        NOT NULL,
            target_title         text        NOT NULL,
            days_remaining       int         NOT NULL,
            next_run_at          timestamptz NOT NULL,
            source_chat_id       bigint      NOT NULL,
            source_message_id    bigint      NOT NULL,
            requester_chat_id    bigint      NOT NULL,
            requester_message_id bigint      NOT NULL,
            requester_user_id    bigint      NOT NULL,
            created_at           timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (group_id, post_id),
            FOREIGN KEY (group_id) REFERENCES groups (group_id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        "SELECT create_distributed_table('scheduled_posts', 'group_id', colocate_with => 'groups')"
    )

    # The delivery cron's due sweep, and the per-group view behind `max_posts`.
    # `group_id` first so the scan is one shard when a caller knows the group
    # (AGENTS.md §4.2).
    op.execute("CREATE INDEX scheduled_posts_due_idx ON scheduled_posts (group_id, next_run_at)")

    # `schedule_post`'s one-live-campaign-per-source-channel rule (v1 :238-242).
    op.execute(
        "CREATE INDEX scheduled_posts_origin_idx ON scheduled_posts (group_id, origin_title)"
    )

    # `util_deletereposts` and the reply relay both filter on the requester,
    # which is deliberately *not* the distribution column: the rows a group
    # cancels are spread across every group its campaign targeted, so no
    # `group_id` predicate would be correct. Both statements therefore fan out
    # across shards. That is allowed here and nowhere hotter — each is a rare,
    # human-triggered, single-table statement, not a repartition join.
    op.execute("CREATE INDEX scheduled_posts_requester_idx ON scheduled_posts (requester_chat_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS scheduled_posts")
