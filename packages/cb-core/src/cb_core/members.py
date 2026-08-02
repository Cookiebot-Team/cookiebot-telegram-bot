"""Who is in a group — the registry v1 kept in Mongo and every fun command reads.

v1 rebuilt this on every single message. `check_new_name`
(`../COOKIEBOT-Telegram-Group-Bot/Bot/UserRegisters.py:64-88`) took the sender off
the update, upserted a `users` document through the Java backend
(`get_user_info`, `:33-61`), and then, for a group chat, appended the **username**
to a per-chat `registers/{chat_id}` document — a bare list of username strings:

    if username and username not in str(members):
        post_request_backend(f"registers/{chat_id}/users", {"user": username, "date": ''})

`str(members)` is not a typo in this docstring; v1 really does the containment
check against the *repr* of the list, so a username that happens to be a
substring of another member's name is silently never registered. That one is a
defect, not a behaviour, and it is not reproduced (see "What changed" below).

Leaving reverses it — `left_chat_member` (`:92-96`) deletes the username from the
register. v2 sets `group_members.left_at` instead of deleting the row, so
`joined_at` survives a leave/rejoin cycle for `core_mediarestrict`, which reads it
(`packages/cb-gateway/src/cb_gateway/handlers/mediarestrict.py:145`).

## Why this is a shared module and not part of one handler

Four ported-or-pending features read it: `fun_ship` (two random members),
`util_everyone` (all of them), `util_birthday` / `util_nextbirthday` (the `users`
rows this writes, via the generated `birth_month` / `birth_day` columns). v1 paid
for the registry once per message and shared it the same way; the difference is
that v1's copy was a process-local dict per bot process
(`UserRegisters.py:11-12`, five processes, no invalidation) and this one is the
database.

## What changed, deliberately

| v1 | here | why |
|---|---|---|
| register holds usernames only | rows hold `user_id`, `users` holds the username | a user who changes username disappeared from v1's register and re-registered as a stranger |
| membership check is `username not in str(members)` | primary key `(group_id, user_id)` | v1's substring check silently skipped real members |
| leaving deletes the register entry | `left_at` is stamped | `group_members.joined_at` is load-bearing for media restriction |
| whole register reloaded when it exceeds 2x the member count (`:24-28`) | nothing | that reload existed because v1 could not delete reliably; a primary key can |

Unchanged on purpose: **bots are registered like anyone else.** v1 filters
nothing (`check_new_name` runs for whatever `msg['from']` says), so a chatty
second bot in the group can be shipped. `users.is_bot` is recorded so a future
feature can filter, but no reader does today.
"""

from __future__ import annotations

from dataclasses import dataclass

from cb_core import db
from cb_core.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class MemberIdentity:
    """The fields v1's `get_user_info` carried (`UserRegisters.py:33-34`)."""

    user_id: int
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    language_code: str | None = None
    is_bot: bool = False


@dataclass(frozen=True, slots=True)
class MemberRef:
    """One row of `roster()` — deliberately the smallest shape both consumers
    need. `util_everyone`'s handler projects out `username` for the ping text;
    its worker projects out `user_id` for the DM. One roster, two projections
    (design R1.2) — do not add a second query for either half alone."""

    user_id: int
    username: str | None = None


# v1 skipped the backend round trip when its `cache_users` copy already agreed
# with the update (`UserRegisters.py:35-36`). The same guard matters more here:
# `users` is a Citus **reference table**, so every write is replicated to every
# node, and a chatty group would otherwise pay that on every message. Keyed by
# the whole identity, so a rename still writes.
_seen_identities: set[MemberIdentity] = set()

# Membership is monotonic while the member stays: one write per (group, user)
# per process, mirroring `cb_core.groups._ensured`. A leave clears the entry so
# the rejoin writes again.
_seen_memberships: set[tuple[int, int]] = set()


_UPSERT_USER = """
INSERT INTO users (user_id, username, first_name, last_name, language_code, is_bot)
VALUES ($1, $2, $3, $4, $5, $6)
ON CONFLICT (user_id) DO UPDATE
   SET username      = COALESCE(EXCLUDED.username, users.username),
       first_name    = COALESCE(EXCLUDED.first_name, users.first_name),
       last_name     = COALESCE(EXCLUDED.last_name, users.last_name),
       language_code = COALESCE(EXCLUDED.language_code, users.language_code),
       is_bot        = EXCLUDED.is_bot,
       updated_at    = EXCLUDED.updated_at
"""

# `EXCLUDED.updated_at`, never `now()`: Citus rejects a non-IMMUTABLE function in
# a `DO UPDATE SET` on a distributed table —
#     functions used in the DO UPDATE SET clause of INSERTs on distributed
#     tables must be marked IMMUTABLE
# — and this table is replicated to every node, so it is subject to the same
# rule. `EXCLUDED.updated_at` carries the column's own `DEFAULT now()`, evaluated
# once on the coordinator. Migration 0001's rollups (`cb_rollup_day`) and
# `MediaService` already had to make exactly this substitution; see HANDOFF.md §1.

# COALESCE, not EXCLUDED, for everything except `is_bot`: v1 only overwrote a
# field when the update carried a non-null value for it
# (`UserRegisters.py:57-59`), so a Telegram update that omits `last_name` must
# not erase a name recorded earlier. `is_bot` is always known from the update.

_UPSERT_MEMBERSHIP = """
INSERT INTO group_members (group_id, user_id)
VALUES ($1, $2)
ON CONFLICT (group_id, user_id) DO UPDATE
   SET left_at = NULL
 WHERE group_members.left_at IS NOT NULL
"""

# **`joined_at` is not in that INSERT, and that is the whole point.** Hearing
# from someone proves they are here, not when they arrived — and almost every
# member the registry records was already in the group before the bot was.
# `core_mediarestrict` restricts media from anyone whose `joined_at` is inside
# the configured window, so claiming `now()` here would mute a five-year member
# on their first message after a deploy. Migration `0004` made the column
# nullable for exactly this; `first_seen_at` (defaulted) records what we do know,
# and the join handler is the only writer of `joined_at`.
#
# It is not touched on conflict either: a rejoin must not reset the clock for a
# member whose join *was* witnessed. Clearing `left_at` only when it is set keeps
# the common path a no-op rather than a pointless row update.

_MARK_LEFT = """
UPDATE group_members SET left_at = now()
 WHERE group_id = $1 AND user_id = $2 AND left_at IS NULL
"""

# Single shard: `group_id` leads the predicate, and `users` is a reference table
# so the join is node-local (AGENTS.md §4.4). `ORDER BY random()` over one
# group's members is the same shape `MediaService.random` already uses for the
# per-group media pool, and for the same reason: the alternative is v1's
# "load the entire collection into the application and pick one".
_RANDOM_USERNAMES = """
SELECT u.username
  FROM group_members m
  JOIN users u ON u.user_id = m.user_id
 WHERE m.group_id = $1
   AND m.left_at IS NULL
   AND u.username IS NOT NULL
 ORDER BY random()
 LIMIT $2
"""

_COUNT_MEMBERS = """
SELECT count(*) AS n FROM group_members WHERE group_id = $1 AND left_at IS NULL
"""

# The batched read `util_everyone` replaces v1's N+1 with (`UserRegisters.py:129`
# — one `GET users?username=` per member). Same shape as `_RANDOM_USERNAMES`
# minus `ORDER BY random() LIMIT`: single shard, `users` joined as a reference
# table so the join stays node-local (AGENTS.md §4.4). `ORDER BY user_id` makes
# the ping text reproducible in tests instead of depending on physical row order.
_ROSTER = """
SELECT m.user_id, u.username
  FROM group_members m
  JOIN users u ON u.user_id = m.user_id
 WHERE m.group_id = $1
   AND m.left_at IS NULL
 ORDER BY m.user_id
"""


async def record(group_id: int, identity: MemberIdentity) -> None:
    """v1's `check_new_name` (`UserRegisters.py:64-88`), as one call.

    Bookkeeping on the reply path, so it must never be the reason an update
    fails: a database that is down degrades to "this member is not registered
    yet", exactly the state v1 was in whenever its backend call failed
    (`get_request_backend` returns `''` and the caller treats it as an empty
    register, `:18-19`).
    """
    if identity not in _seen_identities:
        try:
            await db.execute(
                _UPSERT_USER,
                identity.user_id,
                identity.username,
                identity.first_name,
                identity.last_name,
                identity.language_code,
                identity.is_bot,
                name="members_upsert_user",
            )
        except Exception as exc:  # noqa: BLE001 - see docstring: never break the reply
            log.warning("members.user_upsert_failed", error=str(exc))
            return
        _seen_identities.add(identity)

    if group_id and (group_id, identity.user_id) not in _seen_memberships:
        try:
            await db.execute(
                _UPSERT_MEMBERSHIP, group_id, identity.user_id, name="members_upsert_membership"
            )
        except Exception as exc:  # noqa: BLE001 - see above
            log.warning("members.membership_upsert_failed", error=str(exc))
            return
        _seen_memberships.add((group_id, identity.user_id))


async def mark_left(group_id: int, user_id: int) -> None:
    """v1's `left_chat_member` (`UserRegisters.py:92-96`), minus the row deletion."""
    _seen_memberships.discard((group_id, user_id))
    try:
        await db.execute(_MARK_LEFT, group_id, user_id, name="members_mark_left")
    except Exception as exc:  # noqa: BLE001 - bookkeeping, never a reply
        log.warning("members.mark_left_failed", error=str(exc))


async def random_usernames(group_id: int, count: int) -> list[str]:
    """`count` distinct registered usernames from this group, in random order.

    Returns fewer than `count` — possibly none — when the group has not
    registered that many yet; callers decide what that means (`fun_ship` answers
    with v1's `no_ship` string). Usernames come back **without** a leading `@`,
    the way v1's register stored them and its templates re-prefix them.
    """
    if count < 1:
        return []
    try:
        rows = await db.fetch(_RANDOM_USERNAMES, group_id, count, name="members_random")
    except Exception as exc:  # noqa: BLE001 - a fun command must not 500 on a db blip
        log.warning("members.random_failed", error=str(exc))
        return []
    return [row["username"] for row in rows]


async def roster(group_id: int) -> tuple[MemberRef, ...]:
    """The whole current membership, one query — `util_everyone`'s roster read.

    v1 fetched the register (a bare list of usernames) and then made one
    `GET users?username=` backend call per member to resolve it
    (`UserRegisters.py:129`); with `group_members` already carrying the user
    id, that N+1 disappears into a single single-shard join. A database
    failure degrades to an empty roster rather than a 500 — the caller reads
    it the same way v1's caller treated an empty register (`:18-19`).
    """
    try:
        rows = await db.fetch(_ROSTER, group_id, name="members_roster")
    except Exception as exc:  # noqa: BLE001 - a fun/util command must not 500 on a db blip
        log.warning("members.roster_failed", error=str(exc))
        return ()
    return tuple(MemberRef(user_id=row["user_id"], username=row["username"]) for row in rows)


async def count(group_id: int) -> int:
    """How many members are currently registered — `util_everyone` sizes its
    fan-out with this, and tests assert on it."""
    row = await db.fetchrow(_COUNT_MEMBERS, group_id, name="members_count")
    return int(row["n"]) if row is not None else 0


def reset_cache() -> None:
    """Drop the process-local write-skip caches. For tests and for the importer,
    which writes the same rows behind this module's back."""
    _seen_identities.clear()
    _seen_memberships.clear()


__all__ = [
    "MemberIdentity",
    "MemberRef",
    "count",
    "mark_left",
    "random_usernames",
    "record",
    "reset_cache",
    "roster",
]
