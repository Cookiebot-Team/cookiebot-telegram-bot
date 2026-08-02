"""group_members: "we have seen this member" is not "we watched them join"

`cb_core.members` (the registry v1 kept in Mongo, written on every message —
`../COOKIEBOT-Telegram-Group-Bot/Bot/UserRegisters.py:64-88`) has to record
members the bot never watched arrive: everyone who was already in the group when
it was added, which is almost everyone.

`joined_at` cannot answer for those. It was `NOT NULL DEFAULT now()`, so a
registry insert would have claimed that a member who has been in the group for
five years arrived just now — and `core_mediarestrict` restricts media from
anyone whose `joined_at` is inside the configured window
(`cb_gateway/handlers/mediarestrict.py:225-234`). Their first message after the
deploy would have got them muted for ten minutes. That handler already has the
right behaviour for "we do not know": its own docstring calls out "a member who
joined before this feature existed" and fails open on a missing row. This makes
that state representable instead of forcing a wrong answer into the column.

After this migration:

- `joined_at` is nullable and has no default. The **join event** sets it
  (`mediarestrict._record_join`); nothing else may.
- `first_seen_at` — already in the schema, already defaulted — is what the
  registry writes. "When did we first hear from them", which is exactly what it
  was named for.

Existing rows are untouched: every one of them was written by the join handler
or the v1 importer, so their `joined_at` is real.

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE group_members ALTER COLUMN joined_at DROP NOT NULL")
    op.execute("ALTER TABLE group_members ALTER COLUMN joined_at DROP DEFAULT")


def downgrade() -> None:
    # The NOT NULL cannot come back while unknown-join rows exist, and inventing
    # a join time for them is the exact defect this migration removes. Falling
    # back to `first_seen_at` is the one honest answer: it is never later than
    # the real join, so a restored constraint under-enforces rather than muting
    # a long-standing member.
    op.execute("UPDATE group_members SET joined_at = first_seen_at WHERE joined_at IS NULL")
    op.execute("ALTER TABLE group_members ALTER COLUMN joined_at SET DEFAULT now()")
    op.execute("ALTER TABLE group_members ALTER COLUMN joined_at SET NOT NULL")
