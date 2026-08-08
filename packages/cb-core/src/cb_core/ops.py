"""Owner-only operations against the whole deployment — x_owner_commands' data half.

v1: the private-chat branch at
`../COOKIEBOT-Telegram-Group-Bot/Bot/COOKIEBOT.py:83-105`, calling
`list_groups` (`Miscellaneous.py:83-112`), `broadcast_message`
(`Miscellaneous.py:114-122`), `leave_and_blacklist` (`universal_funcs.py:320-329`)
and `blacklist_user`/`unblacklist_user` (`:307-313`).

Every statement here deliberately spans shards, and this is the module where
that is least controversial: an owner asking "what groups am I in" or "leave
that one and never come back" is asking a question *about* the shard key, not
one that can be answered inside it. All of them are rare, human-triggered and
single-table — AGENTS.md §4.4's sanctioned shape, the same one
`scheduled_posts.delete_by_requester` already uses — and `list_groups` is
paged rather than unbounded.
"""

from __future__ import annotations

import msgspec

from cb_core import db
from cb_core.logging import get_logger

log = get_logger("cb.ops")


class GroupSummary(msgspec.Struct, frozen=True):
    """One line of `/grupos`. v1 printed `f"{id} - {title}"`
    (`Miscellaneous.py:99`), fetching the title from Telegram one `getChat`
    per group with a `sleep(0.4)` between them; the title is a column here."""

    group_id: int
    title: str


_LIST_GROUPS = """
SELECT group_id, coalesce(title, '') AS title
  FROM groups
 ORDER BY group_id
 LIMIT $1 OFFSET $2
"""

_COUNT_GROUPS = "SELECT count(*) AS n FROM groups"

_ALL_GROUP_IDS = "SELECT group_id FROM groups ORDER BY group_id"

_BLACKLIST_ADD = """
INSERT INTO blacklist (subject_id, kind, reason, source)
VALUES ($1, $2, $3, 'manual')
ON CONFLICT (subject_id) DO UPDATE
   SET kind = EXCLUDED.kind, reason = EXCLUDED.reason, source = 'manual'
"""

_BLACKLIST_REMOVE = "DELETE FROM blacklist WHERE subject_id = $1"

_FORGET_GROUP = "DELETE FROM groups WHERE group_id = $1"


async def list_groups(*, limit: int = 100, offset: int = 0) -> tuple[GroupSummary, ...]:
    """A page of the groups this deployment knows about.

    **Paged, where v1 was not.** v1 fetched every group, called `getChat` on
    each with a 0.4s sleep, and sent one Telegram message per group
    (`Miscellaneous.py:93-103`) — thirty minutes of blocked thread and a
    thousand messages for a thousand groups. FEATURE-MAP D11 names exactly
    this ("no pagination anywhere").
    """
    rows = await db.fetch(_LIST_GROUPS, limit, offset, name="ops_list_groups")
    return tuple(GroupSummary(group_id=row["group_id"], title=row["title"]) for row in rows)


async def count_groups() -> int:
    """v1's `groups.total` number (`Miscellaneous.py:105`), minus the groups it
    had just discovered were gone — see `list_groups`' docstring for why v2
    does not discover that here."""
    row = await db.fetchrow(_COUNT_GROUPS, name="ops_count_groups")
    return int(row["n"]) if row is not None else 0


async def all_group_ids() -> tuple[int, ...]:
    """Every group, for the broadcast fan-out. Unpaged on purpose: the caller
    is a worker job that must reach all of them, and it pages *itself* through
    a deferred job per group."""
    rows = await db.fetch(_ALL_GROUP_IDS, name="ops_all_groups")
    return tuple(int(row["group_id"]) for row in rows)


async def blacklist_add(subject_id: int, *, kind: str = "user", reason: str | None = None) -> None:
    """v1's `blacklist_user` (`universal_funcs.py:307-309`).

    `kind` distinguishes a banned user from a banned chat — v1 posted both to
    the same `blacklist/{id}` endpoint with no way to tell them apart, which
    is why `leave_and_blacklist` and `blacklist_user` were indistinguishable
    in the store. The column already existed here; this is its first writer
    outside the doomlist.
    """
    await db.execute(_BLACKLIST_ADD, subject_id, kind, reason, name="ops_blacklist_add")


async def blacklist_remove(subject_id: int) -> bool:
    """v1's `unblacklist_user` (`:311-313`). Returns whether a row was removed,
    so the owner is told "not listed" rather than a bare confirmation."""
    result = await db.execute(_BLACKLIST_REMOVE, subject_id, name="ops_blacklist_remove")
    parts = result.split()
    return bool(parts and parts[-1].isdigit() and int(parts[-1]) > 0)


async def forget_group(group_id: int) -> None:
    """v1's three deletes in `leave_and_blacklist` (`:322-324`: `registers`,
    `configs`, `groups`) — one statement here, because every tenant-scoped
    table has an `ON DELETE CASCADE` foreign key to `groups`. That is also
    the difference between v1's version and this one: v1 deleted the three
    collections it remembered to name, and left the rest (`randomdatabase`,
    scheduled posts, giveaways) pointing at a group it had just left.
    """
    await db.execute(_FORGET_GROUP, group_id, name="ops_forget_group")


__all__ = [
    "GroupSummary",
    "all_group_ids",
    "blacklist_add",
    "blacklist_remove",
    "count_groups",
    "forget_group",
    "list_groups",
]
