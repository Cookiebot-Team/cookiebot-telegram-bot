"""Group admin resolution: fetch, cache, persist, and the anonymous-sender fix.

`group_admins` has existed since migration 0001 and nothing populated it. v1's
equivalent (`Configurations.py:get_admins`) kept an unbounded, unlocked,
never-expiring dict *per process* (FEATURE-MAP D6) with no failure path at all:
a `getChatAdministrators` error propagated to `thread_function`'s single broad
`except Exception` (`COOKIEBOT.py:329-330`), which mailed the bot owner a
traceback and silently dropped the update. Here the cache is shared (L1 process
+ L2 Valkey), a refresh from any replica fixes every replica, the resolved set
is persisted so it survives a cache flush or a Telegram outage, and a Telegram
failure degrades to "last known admins" or "nobody", never to "everyone" and
never to silence.

v1 also shipped a real defect around anonymous admins: `configurar` (the
`/config` entry point), `giveaways_ask`, and the `Pub`/`GIVEAWAY` callback
handlers check `str(from_id) not in listaadmins_id`, and an anonymous admin's
`from.id` is Telegram's synthetic `GroupAnonymousBot` (1087968824) rather than
the admin's own id — so a genuine admin posting anonymously was always rejected,
shown a permission-denied message, and (in `configurar` specifically) sent
`Static/remove_anonymous_tutorial.mp4` telling them to turn off a Telegram
feature that was never the problem (`Configurations.py:141-144`). Six other v1
call sites get this right by accident, by short-circuiting on `'sender_chat' in
msg` (Telegram only attaches `sender_chat` = the group to a message once it has
already verified the sender is an admin with anonymity on). `resolve_actor`
generalises the accidental-good behaviour and removes the defect: an anonymous
sender is trusted as an admin unconditionally, because there is no real user id
left to check against our own cache anyway. See docs/contracts/admins.md.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Protocol

from cb_core import cache, db, metrics
from cb_core.logging import get_logger
from cb_core.settings import get_settings

log = get_logger("cb.admins")

#: Telegram's synthetic user id for a message sent as an anonymous group admin.
ANONYMOUS_BOT_ID: int = 1087968824

_CACHE = "admins"
_CACHE_PREFIX = "cb:admins:"


@dataclass(frozen=True, slots=True)
class Admin:
    user_id: int
    role: str  # 'creator' | 'administrator'
    can_restrict_members: bool
    can_delete_messages: bool


@dataclass(frozen=True, slots=True)
class ActorCheck:
    user_id: int | None
    is_admin: bool
    anonymous: bool  # true when Telegram hid the real sender


class _ChatMemberLike(Protocol):
    status: str
    user: Any


class _BotLike(Protocol):
    async def get_chat_administrators(self, chat_id: int) -> list[Any]: ...


class _ChatLike(Protocol):
    id: int


class _UserLike(Protocol):
    id: int


class _MessageLike(Protocol):
    """The subset of an aiogram `Message` this module actually reads.

    cb_core does not depend on aiogram (kept framework-agnostic), so callers pass
    the real `Message` structurally rather than by importing its type.
    """

    chat: _ChatLike
    sender_chat: _ChatLike | None
    from_user: _UserLike | None


# Process-local L1: {group_id: (expires_at_monotonic, admins)}. A module-level
# clock function so tests can move time without a real sleep.
_l1: dict[int, tuple[float, tuple[Admin, ...]]] = {}
_now = time.monotonic


def _l1_get(group_id: int) -> tuple[Admin, ...] | None:
    entry = _l1.get(group_id)
    if entry is None:
        return None
    expires_at, value = entry
    if _now() >= expires_at:
        _l1.pop(group_id, None)
        return None
    return value


def _l1_set(group_id: int, value: tuple[Admin, ...]) -> None:
    ttl = get_settings().config_cache_l1_seconds
    _l1[group_id] = (_now() + ttl, value)


def _admin_to_dict(admin: Admin) -> dict[str, Any]:
    return {
        "user_id": admin.user_id,
        "role": admin.role,
        "can_restrict_members": admin.can_restrict_members,
        "can_delete_messages": admin.can_delete_messages,
    }


async def _l2_get(group_id: int) -> tuple[Admin, ...] | None:
    """A cache outage is a miss, not an error.

    Valkey being down must cost a Telegram call, never an admin check — this
    module promises callers it never raises, and `is_admin` returning an
    exception would take an admin-gated command down with it.
    """
    try:
        payload = await cache.get_json(f"{_CACHE_PREFIX}{group_id}")
    except Exception as exc:  # noqa: BLE001 - cache is optional; degrade to Telegram
        log.warning("admins.l2_read_failed", group_id=group_id, error=str(exc))
        metrics.cache_lookups_total.labels(cache=_CACHE, layer="l2", outcome="error").inc()
        return None
    if payload is None:
        return None
    return tuple(Admin(**row) for row in payload)


async def _l2_set(group_id: int, value: tuple[Admin, ...]) -> None:
    ttl = get_settings().admin_cache_seconds
    try:
        await cache.set_json(f"{_CACHE_PREFIX}{group_id}", [_admin_to_dict(a) for a in value], ttl)
    except Exception as exc:  # noqa: BLE001 - failing to warm a cache is not a failure
        log.warning("admins.l2_write_failed", group_id=group_id, error=str(exc))
        metrics.cache_lookups_total.labels(cache=_CACHE, layer="l2", outcome="error").inc()


def _parse_admins(raw: list[Any]) -> tuple[tuple[Admin, ...], tuple[tuple[int, str, bool], ...]]:
    """Returns (public Admin tuple, (user_id, role, is_anonymous) for the DB write).

    `is_anonymous` here is Telegram's per-admin "remain anonymous" toggle
    (`ChatMemberAdministrator.is_anonymous` / `ChatMemberOwner.is_anonymous`) — a
    property of the admin's role, not of any particular message — persisted into
    `group_admins.anonymous` but deliberately not part of the public `Admin`
    dataclass, which mirrors only what M1 handlers need.
    """
    resolved: list[Admin] = []
    for_db: list[tuple[int, str, bool]] = []
    for member in raw:
        role = member.status
        user_id = member.user.id
        if role == "creator":
            # ChatMemberOwner has no privilege flags to ask; the creator has
            # every privilege implicitly.
            can_restrict = True
            can_delete = True
        else:
            can_restrict = bool(getattr(member, "can_restrict_members", False))
            can_delete = bool(getattr(member, "can_delete_messages", False))
        resolved.append(
            Admin(
                user_id=user_id,
                role=role,
                can_restrict_members=can_restrict,
                can_delete_messages=can_delete,
            )
        )
        for_db.append((user_id, role, bool(getattr(member, "is_anonymous", False))))
    return tuple(resolved), tuple(for_db)


_SELECT_PERSISTED = "SELECT user_id, role FROM group_admins WHERE group_id = $1 ORDER BY user_id"

_DELETE_GROUP = "DELETE FROM group_admins WHERE group_id = $1"

_INSERT_ADMIN = """
INSERT INTO group_admins (group_id, user_id, role, anonymous)
VALUES ($1, $2, $3, $4)
"""


async def _read_persisted(group_id: int) -> tuple[Admin, ...]:
    """The durable "last known admins" — survives a cache flush and a restart.

    Privilege flags are not stored as columns (migration 0001's `group_admins`
    only has `role`/`anonymous`/`synced_at`), so they are reconstructed
    conservatively: `True` for a creator (implied by the role), `False` for a
    plain administrator (the real values are only known fresh from Telegram).
    """
    try:
        rows = await db.fetch(_SELECT_PERSISTED, group_id, name="admins_read_persisted")
    except Exception as exc:  # noqa: BLE001 - the durable copy is a fallback, not a dependency
        log.warning("admins.persisted_read_failed", group_id=group_id, error=str(exc))
        return ()
    return tuple(
        Admin(
            user_id=row["user_id"],
            role=row["role"],
            can_restrict_members=(row["role"] == "creator"),
            can_delete_messages=(row["role"] == "creator"),
        )
        for row in rows
    )


async def _persist(group_id: int, rows: tuple[tuple[int, str, bool], ...]) -> None:
    """Replace this group's rows in one transaction — never accumulate stale ones.

    Best effort: Telegram has already answered by the time this runs, so a
    database outage costs the durable copy (and the analytics view of it), not
    the admin check the caller is waiting on.
    """
    try:
        async with db.transaction() as conn:
            await conn.execute(_DELETE_GROUP, group_id)
            if rows:
                await conn.executemany(
                    _INSERT_ADMIN,
                    [(group_id, user_id, role, is_anon) for user_id, role, is_anon in rows],
                )
    except Exception as exc:  # noqa: BLE001 - see docstring; the fresh set is already resolved
        log.warning("admins.persist_failed", group_id=group_id, error=str(exc))


async def refresh(bot: _BotLike, group_id: int) -> tuple[Admin, ...]:
    """Force a real Telegram fetch, rewrite the cache and `group_admins`.

    On a Telegram failure, falls back to whatever is already persisted for this
    group; if there is nothing persisted either, nobody is treated as an admin.
    Never raises — an admin-resolution outage must not take down the caller.
    """
    try:
        raw = await bot.get_chat_administrators(group_id)
    except Exception as exc:  # noqa: BLE001 - Telegram is the outside world; degrade, don't crash
        log.warning("admins.telegram_failed", group_id=group_id, error=str(exc))
        metrics.cache_lookups_total.labels(cache=_CACHE, layer="telegram", outcome="error").inc()
        fallback = await _read_persisted(group_id)
        if fallback:
            metrics.cache_lookups_total.labels(cache=_CACHE, layer="fallback", outcome="hit").inc()
            _l1_set(group_id, fallback)
            await _l2_set(group_id, fallback)
            return fallback
        metrics.cache_lookups_total.labels(cache=_CACHE, layer="fallback", outcome="miss").inc()
        log.warning("admins.no_admins_available", group_id=group_id)
        return ()

    metrics.cache_lookups_total.labels(cache=_CACHE, layer="telegram", outcome="hit").inc()
    resolved, for_db = _parse_admins(raw)
    await _persist(group_id, for_db)
    _l1_set(group_id, resolved)
    await _l2_set(group_id, resolved)
    return resolved


async def admins(bot: _BotLike, group_id: int) -> tuple[Admin, ...]:
    """The cached admin set for `group_id`, fetching from Telegram on a full miss."""
    cached = _l1_get(group_id)
    if cached is not None:
        metrics.cache_lookups_total.labels(cache=_CACHE, layer="l1", outcome="hit").inc()
        return cached
    metrics.cache_lookups_total.labels(cache=_CACHE, layer="l1", outcome="miss").inc()

    cached = await _l2_get(group_id)
    if cached is not None:
        metrics.cache_lookups_total.labels(cache=_CACHE, layer="l2", outcome="hit").inc()
        _l1_set(group_id, cached)
        return cached
    metrics.cache_lookups_total.labels(cache=_CACHE, layer="l2", outcome="miss").inc()

    return await refresh(bot, group_id)


async def admin_ids(bot: _BotLike, group_id: int) -> frozenset[int]:
    return frozenset(a.user_id for a in await admins(bot, group_id))


async def is_admin(bot: _BotLike, group_id: int, user_id: int) -> bool:
    return user_id in await admin_ids(bot, group_id)


def is_anonymous_sender(message: _MessageLike) -> bool:
    """True when Telegram hid the real sender behind the group or its anon-bot.

    Tightened from v1's `'sender_chat' not in msg` (presence alone) to
    specifically "`sender_chat` is *this* group" — a message auto-forwarded from
    a linked discussion channel also carries `sender_chat`, but set to the
    channel, not the group, and is not an anonymous group admin.
    """
    sender_chat = getattr(message, "sender_chat", None)
    chat = getattr(message, "chat", None)
    if sender_chat is not None and chat is not None and sender_chat.id == chat.id:
        return True
    from_user = getattr(message, "from_user", None)
    return from_user is not None and from_user.id == ANONYMOUS_BOT_ID


async def resolve_actor(bot: _BotLike, message: _MessageLike) -> ActorCheck:
    """The one call handlers use to gate an admin-only command.

    An anonymous sender is granted `is_admin=True` unconditionally: Telegram only
    lets a message carry `sender_chat` = the group (or a `GroupAnonymousBot`
    sender) once it has already verified the sender is an admin with anonymity
    turned on, and there is no real user id left to check against our own cache
    either way. This is the fix for v1's `/configurar`-shaped defect (see module
    docstring) — an anonymous admin now succeeds instead of being told to turn
    off a Telegram feature that was never the problem.
    """
    if is_anonymous_sender(message):
        return ActorCheck(user_id=None, is_admin=True, anonymous=True)

    from_user = getattr(message, "from_user", None)
    if from_user is None:
        return ActorCheck(user_id=None, is_admin=False, anonymous=False)

    user_id = from_user.id
    group_id = message.chat.id
    admin = await is_admin(bot, group_id, user_id)
    return ActorCheck(user_id=user_id, is_admin=admin, anonymous=False)
