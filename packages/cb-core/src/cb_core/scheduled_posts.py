"""The publisher's schedule — v1's `Publisher.db`, as a distributed table.

v1 kept these rows in a local SQLite file behind one unlocked, shared connection
(`../COOKIEBOT-Telegram-Group-Bot/Bot/Publisher.py:15-17`, FEATURE-MAP D5) and
read the whole table into Python for every question it wanted to ask
(`list_jobs`, `:101-117`). Every predicate below is the SQL equivalent of one of
those Python scans; see `migrations/versions/0005_scheduled_posts.py` for why
v1's composite `name` string became three columns and a `uuid7`.

**Distribution.** `group_id` is the target group. Every statement here filters on
it except two, `delete_by_requester` and `find_by_origin_title`, which cannot:
the rows a campaign owns are spread across every group it targeted, so no
`group_id` predicate would be correct for them. Both fan out across shards, both
are index-backed single-table statements rather than repartition joins, and both
are reached only from a human-triggered command. The comment is on each.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import asyncpg
import msgspec

from cb_core import db
from cb_core.ids import uuid7
from cb_core.logging import get_logger

log = get_logger("cb.scheduled_posts")


class ScheduledPost(msgspec.Struct, frozen=True):
    """One row. Field names are v2's; the v1 column each replaces is in the
    migration's docstring."""

    group_id: int
    post_id: uuid.UUID
    origin_title: str
    target_title: str
    days_remaining: int
    next_run_at: datetime
    source_chat_id: int
    source_message_id: int
    requester_chat_id: int
    requester_message_id: int
    requester_user_id: int


_COLUMNS = """
    group_id, post_id, origin_title, target_title, days_remaining, next_run_at,
    source_chat_id, source_message_id, requester_chat_id, requester_message_id,
    requester_user_id
"""

_INSERT = f"""
INSERT INTO scheduled_posts ({_COLUMNS})
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
"""

# The cron's sweep. No `group_id` predicate — this one is *meant* to see every
# shard: it is the scheduled worker job AGENTS.md §4.4 names as the sanctioned
# place for a cross-shard read. `LIMIT` bounds one tick's work so a backlog
# drains over several ticks instead of timing the job out.
_DUE_BEFORE = f"""
SELECT {_COLUMNS} FROM scheduled_posts
 WHERE next_run_at <= $1
 ORDER BY next_run_at
 LIMIT $2
"""

_DELETE = "DELETE FROM scheduled_posts WHERE group_id = $1 AND post_id = $2"

_ADVANCE = """
UPDATE scheduled_posts
   SET days_remaining = days_remaining - 1, next_run_at = $3
 WHERE group_id = $1 AND post_id = $2
"""

_COUNT_FOR_GROUP = "SELECT count(*) AS n FROM scheduled_posts WHERE group_id = $1"

# v1 evicted the oldest campaigns by walking the list it was simultaneously
# counting (`:261-267`, D-PF-7), so how many survived depended on iteration
# order. Ordering by `created_at` and deleting an exact count makes it
# deterministic. Single shard: both the outer filter and the subquery carry
# `group_id`.
_TRIM_OLDEST = """
DELETE FROM scheduled_posts
 WHERE group_id = $1
   AND post_id IN (
        SELECT post_id FROM scheduled_posts
         WHERE group_id = $1
         ORDER BY created_at
         LIMIT $2
   )
"""

_DELETE_BY_ORIGIN = "DELETE FROM scheduled_posts WHERE group_id = $1 AND origin_title = $2"

# Cross-shard, on purpose — see the module docstring. `util_deletereposts`.
_DELETE_BY_REQUESTER = "DELETE FROM scheduled_posts WHERE requester_chat_id = $1"

# Cross-shard, on purpose — see the module docstring. The reply relay, which
# knows only the origin channel's title (it reads it off the inline-keyboard
# button `prepare_post` put there). v1 took the first match in table order
# (`:360-369`); `created_at` makes "first" mean the oldest live campaign
# rather than whatever the storage engine happened to return.
_FIND_BY_ORIGIN = f"""
SELECT {_COLUMNS} FROM scheduled_posts
 WHERE origin_title = $1
 ORDER BY created_at
 LIMIT 1
"""


def _row_to_post(row: asyncpg.Record) -> ScheduledPost:
    return ScheduledPost(
        group_id=row["group_id"],
        post_id=row["post_id"],
        origin_title=row["origin_title"],
        target_title=row["target_title"],
        days_remaining=row["days_remaining"],
        next_run_at=row["next_run_at"],
        source_chat_id=row["source_chat_id"],
        source_message_id=row["source_message_id"],
        requester_chat_id=row["requester_chat_id"],
        requester_message_id=row["requester_message_id"],
        requester_user_id=row["requester_user_id"],
    )


async def create(
    *,
    group_id: int,
    origin_title: str,
    target_title: str,
    days_remaining: int,
    next_run_at: datetime,
    source_chat_id: int,
    source_message_id: int,
    requester_chat_id: int,
    requester_message_id: int,
    requester_user_id: int,
) -> uuid.UUID:
    """v1's `create_job` (`:94-99`). Returns the new row's id.

    Keyword-only: the v1 signature took nine positional integers in an order
    nothing but the `INSERT` documented, and two of them (`postmail_chat_id`
    vs `second_chatid`) are interchangeable at the type level.
    """
    post_id = uuid7()
    await db.execute(
        _INSERT,
        group_id,
        post_id,
        origin_title,
        target_title,
        days_remaining,
        next_run_at,
        source_chat_id,
        source_message_id,
        requester_chat_id,
        requester_message_id,
        requester_user_id,
        name="scheduled_posts_create",
    )
    return post_id


async def due_before(moment: datetime, *, limit: int = 500) -> tuple[ScheduledPost, ...]:
    """Rows whose `next_run_at` has passed. The cron's only read."""
    rows = await db.fetch(_DUE_BEFORE, moment, limit, name="scheduled_posts_due")
    return tuple(_row_to_post(row) for row in rows)


async def delete(group_id: int, post_id: uuid.UUID) -> None:
    """v1's `delete_job` (`:119-122`), by key rather than by formatted name."""
    await db.execute(_DELETE, group_id, post_id, name="scheduled_posts_delete")


async def advance_or_expire(post: ScheduledPost, next_run_at: datetime) -> bool:
    """v1's `:335-339`: one day is spent on every attempt, successful or not.

    Returns `True` when the row survived, `False` when this was its last day and
    it was deleted. v1 decrements *before* attempting the forward (D-PF-9,
    preserved): the alternative is retrying a permanently broken target forever.
    """
    if post.days_remaining <= 1:
        await delete(post.group_id, post.post_id)
        return False
    await db.execute(
        _ADVANCE, post.group_id, post.post_id, next_run_at, name="scheduled_posts_advance"
    )
    return True


async def count_for_group(group_id: int) -> int:
    """How many campaigns currently target this group — the `max_posts` input."""
    row = await db.fetchrow(_COUNT_FOR_GROUP, group_id, name="scheduled_posts_count")
    return int(row["n"]) if row is not None else 0


async def trim_to_max(group_id: int, max_posts: int) -> int:
    """Evict the oldest campaigns so that inserting one more leaves `max_posts`.

    v1's intent (`:261-267`) with deterministic arithmetic — see `_TRIM_OLDEST`.
    Returns how many rows were removed; `0` whenever the group is under its cap,
    which is every group at the default `max_posts` of 9999.
    """
    if max_posts <= 0:
        return 0
    live = await count_for_group(group_id)
    excess = live + 1 - max_posts
    if excess <= 0:
        return 0
    result = await db.execute(_TRIM_OLDEST, group_id, excess, name="scheduled_posts_trim")
    return _rows_affected(result)


async def delete_by_origin_title(group_id: int, origin_title: str) -> int:
    """One live campaign per source channel, per target group (v1 `:238-242`)."""
    result = await db.execute(
        _DELETE_BY_ORIGIN, group_id, origin_title, name="scheduled_posts_delete_origin"
    )
    return _rows_affected(result)


async def delete_by_requester(requester_chat_id: int) -> int:
    """`util_deletereposts`. Cross-shard by necessity — see the module docstring.

    v1 read every row and issued one `DELETE ... WHERE name = ?` per match
    (`:322-324`, D-DR-1); this is the same set in one statement.
    """
    result = await db.execute(
        _DELETE_BY_REQUESTER, requester_chat_id, name="scheduled_posts_delete_requester"
    )
    return _rows_affected(result)


async def find_by_origin_title(origin_title: str) -> ScheduledPost | None:
    """The reply relay's lookup. Cross-shard — see the module docstring."""
    row = await db.fetchrow(_FIND_BY_ORIGIN, origin_title, name="scheduled_posts_find_origin")
    return _row_to_post(row) if row is not None else None


def _rows_affected(result: str) -> int:
    """asyncpg returns the command tag (`"DELETE 3"`); callers want the number."""
    parts = result.split()
    return int(parts[-1]) if parts and parts[-1].isdigit() else 0
