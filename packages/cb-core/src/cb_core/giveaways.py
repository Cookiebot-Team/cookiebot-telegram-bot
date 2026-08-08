"""Live giveaways and their entrants — v1's `Giveaways.db`, as two distributed tables.

v1 (`../COOKIEBOT-Telegram-Group-Bot/Bot/Giveaways.py`) held this in a local
SQLite file behind a process-wide `RLock` and a `threading.local()` connection
(`:14-23`, FEATURE-MAP D5's shape), keyed rows by the Telegram `message_id`
alone, and stored the entrants as one comma-joined string it rewrote on every
press. Each of those is a column or a predicate here instead — see
`migrations/versions/0006_giveaways.py` for why the participants became their
own table.

**Distribution.** Every statement in this module filters on `group_id`, the
shard key. A callback press always knows its chat, so unlike `scheduled_posts`
there is no read here that has to fan out.
"""

from __future__ import annotations

import uuid

import asyncpg
import msgspec

from cb_core import db
from cb_core.ids import uuid7


class Giveaway(msgspec.Struct, frozen=True):
    """One live giveaway."""

    group_id: int
    giveaway_id: uuid.UUID
    message_id: int
    creator_id: int
    prize: str
    winners_wanted: int


class Participant(msgspec.Struct, frozen=True):
    """One entrant. `display_name` is what v1 stored in its joined string —
    `"@" + username`, or the first name when the account has no username
    (`Giveaways.py:77`) — kept because it is what the winner announcement
    prints. `user_id` is what identity actually means, and is the key."""

    user_id: int
    display_name: str


_COLUMNS = "group_id, giveaway_id, message_id, creator_id, prize, winners_wanted"

_INSERT = f"""
INSERT INTO giveaways ({_COLUMNS})
VALUES ($1, $2, $3, $4, $5, $6)
"""

_BY_MESSAGE = f"""
SELECT {_COLUMNS} FROM giveaways
 WHERE group_id = $1 AND message_id = $2
"""

_DELETE = "DELETE FROM giveaways WHERE group_id = $1 AND giveaway_id = $2"

# v1's `UPDATE giveaways SET message_id = ?` (`:156`): once a draw is announced
# the raffle keeps living, attached to the "draw more winners?" message instead.
_REPOINT = """
UPDATE giveaways SET message_id = $3
 WHERE group_id = $1 AND giveaway_id = $2
"""

# `ON CONFLICT DO NOTHING` *is* the "you are already participating" answer —
# v1 scanned its joined string for the display name (`:88`), which both raced
# and confused two members with the same first name. `xmax = 0` is Postgres's
# standard way to distinguish an insert from a no-op conflict in one round trip.
_ENTER = """
INSERT INTO giveaway_participants (group_id, giveaway_id, user_id, display_name)
VALUES ($1, $2, $3, $4)
ON CONFLICT (group_id, giveaway_id, user_id) DO NOTHING
RETURNING xmax = 0 AS inserted
"""

_PARTICIPANTS = """
SELECT user_id, display_name FROM giveaway_participants
 WHERE group_id = $1 AND giveaway_id = $2
 ORDER BY entered_at
"""


def _row_to_giveaway(row: asyncpg.Record) -> Giveaway:
    return Giveaway(
        group_id=row["group_id"],
        giveaway_id=row["giveaway_id"],
        message_id=row["message_id"],
        creator_id=row["creator_id"],
        prize=row["prize"],
        winners_wanted=row["winners_wanted"],
    )


async def create(
    *,
    group_id: int,
    message_id: int,
    creator_id: int,
    prize: str,
    winners_wanted: int,
) -> uuid.UUID:
    """v1's `INSERT INTO giveaways VALUES (...)` (`:67`), with a real key.

    Keyword-only: v1 passed six positional values whose order only the SQL
    documented, three of them plain integers.
    """
    giveaway_id = uuid7()
    await db.execute(
        _INSERT,
        group_id,
        giveaway_id,
        message_id,
        creator_id,
        prize,
        winners_wanted,
        name="giveaways_create",
    )
    return giveaway_id


async def by_message(group_id: int, message_id: int) -> Giveaway | None:
    """The lookup behind every button press. v1: `WHERE message_id = ?`
    (`:81,107,169`) with no chat predicate at all — see the migration."""
    row = await db.fetchrow(_BY_MESSAGE, group_id, message_id, name="giveaways_by_message")
    return _row_to_giveaway(row) if row is not None else None


async def delete(group_id: int, giveaway_id: uuid.UUID) -> None:
    """v1's `DELETE FROM giveaways WHERE message_id = ?` (`:121,169`).

    The participants go with it: the foreign key is `ON DELETE CASCADE`, and
    both tables are colocated, so the cascade is node-local.
    """
    await db.execute(_DELETE, group_id, giveaway_id, name="giveaways_delete")


async def repoint(group_id: int, giveaway_id: uuid.UUID, message_id: int) -> None:
    """Attach a live giveaway to a newly posted message (v1 `:156`)."""
    await db.execute(_REPOINT, group_id, giveaway_id, message_id, name="giveaways_repoint")


async def enter(group_id: int, giveaway_id: uuid.UUID, *, user_id: int, display_name: str) -> bool:
    """Register an entrant. `False` means they were already in.

    v1 read the joined participant string, checked membership in Python and
    wrote the whole string back (`:81-94`) — two presses in the same instant
    lost one of them. This is one statement.
    """
    row = await db.fetchrow(
        _ENTER, group_id, giveaway_id, user_id, display_name, name="giveaways_enter"
    )
    return row is not None and bool(row["inserted"])


async def participants(group_id: int, giveaway_id: uuid.UUID) -> tuple[Participant, ...]:
    """Everyone currently entered, in entry order."""
    rows = await db.fetch(_PARTICIPANTS, group_id, giveaway_id, name="giveaways_participants")
    return tuple(
        Participant(user_id=row["user_id"], display_name=row["display_name"]) for row in rows
    )


__all__ = [
    "Giveaway",
    "Participant",
    "by_message",
    "create",
    "delete",
    "enter",
    "participants",
    "repoint",
]
