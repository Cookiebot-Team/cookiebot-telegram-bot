"""giveaways: v1's raffle queue, out of a second local SQLite file

v1 kept every live giveaway in `Giveaways.db`
(`../COOKIEBOT-Telegram-Group-Bot/Bot/Giveaways.py:17-23`) — a second SQLite
file next to `Publisher.db`, opened per thread through `threading.local()` and
serialised by a process-wide `RLock`. Same class of problem migration 0005
describes for the publisher: the schedule lived on one host, was invisible to
any other process, and did not survive that host being replaced.

Two tables, not one. v1 stored the entrants as a **comma-joined string** in a
`participants TEXT` column and rewrote the whole column on every entry
(`:92-93`), which is a lost update the moment two people press the button in
the same millisecond, and which identifies a participant by display name — so
two members whose first name is "Alex" are one entrant. Here each entry is a
row keyed by `user_id`, so "already participating" is a primary-key conflict
rather than a substring scan, and concurrent presses cannot overwrite each
other.

Distributed on `group_id`, colocated with `groups`, so the giveaway and its
participants join node-local (AGENTS.md §4). Every read below carries
`group_id`: a callback press always knows the chat it came from, which is what
v1's `WHERE message_id = ?` (`:81`) did not — that lookup was global, and two
groups whose giveaway messages happened to share a message id would have
answered each other's presses.

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE giveaways (
            group_id        bigint      NOT NULL,
            giveaway_id     uuid        NOT NULL,
            message_id      bigint      NOT NULL,
            creator_id      bigint      NOT NULL,
            prize           text        NOT NULL,
            winners_wanted  int         NOT NULL,
            created_at      timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (group_id, giveaway_id),
            FOREIGN KEY (group_id) REFERENCES groups (group_id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        "SELECT create_distributed_table('giveaways', 'group_id', colocate_with => 'groups')"
    )

    # Every callback press resolves the giveaway by the message its button is
    # attached to. Unique because a message id is unique within a chat, and
    # `end` re-points a live giveaway at the "draw more winners?" message it
    # posts (v1 `:156`) — the uniqueness is what makes that an UPDATE rather
    # than a second live row for the same raffle.
    op.execute("CREATE UNIQUE INDEX giveaways_message_idx ON giveaways (group_id, message_id)")

    op.execute(
        """
        CREATE TABLE giveaway_participants (
            group_id     bigint      NOT NULL,
            giveaway_id  uuid        NOT NULL,
            user_id      bigint      NOT NULL,
            display_name text        NOT NULL,
            entered_at   timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (group_id, giveaway_id, user_id),
            FOREIGN KEY (group_id, giveaway_id)
                REFERENCES giveaways (group_id, giveaway_id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        "SELECT create_distributed_table('giveaway_participants', 'group_id',"
        " colocate_with => 'groups')"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS giveaway_participants")
    op.execute("DROP TABLE IF EXISTS giveaways")
