"""core_stickerspam — anti sticker-flood: warn once, then delete, per group.

v1: `Cooldowns.py:12-22` `sticker_anti_spam`, dispatched unconditionally for every
sticker message (`COOKIEBOT.py:179-180`), reached only after the private-chat
early return at `COOKIEBOT.py:106-110` — so v1 never ran this in a private chat
either.

QA: `Cookiebot-QA/features/core_stickerspam.feature` -> `qa/features/core_stickerspam.feature`.
Contract: `docs/contracts/core_stickerspam.md` (read that first for the full
v1/v2 table, the FEATURE-MAP D6 defect this deliberately fixes, and the
cache-outage fail-open decision).

The counting itself is the one deliberate behaviour change. v1 kept
`last_used_sticker[chat_id]` in a module-level dict with no time bound at all —
it only ever went back to 0 when `sticker_cooldown_updates` happened to fire on
some *other* message in the same process (`COOKIEBOT.py:317-318`), and five v1
processes each kept their own copy. Ported literally, that dict would just be
D6 again in a new shape: five gateway replicas, five divergent counts, and no
guaranteed reset. `cb_core.cache.incr_window` replaces it with one atomic,
shared, time-boxed counter (`ctx.config.sticker_spam_window_s`, a v2 addition —
v1 had no window at all). The warn-at-`==`, delete-at-`>` thresholds themselves
are ported byte-for-byte.
"""

from __future__ import annotations

import contextlib
from typing import cast

from aiogram import Bot, F, Router
from aiogram.types import Message

from cb_core import cache
from cb_core.logging import get_logger
from cb_gateway.context import context_for, t

router = Router(name="stickerspam")

log = get_logger("cb.stickerspam")

_KEY_PREFIX = "cb:stickerspam:"


def _key(group_id: int) -> str:
    # Per-group, not per-user — v1 keyed `last_used_sticker` by `chat_id` alone
    # (`Cooldowns.py:8`), so one user's stickers count toward the whole group's
    # total and can get a *different* user's next sticker deleted. Preserved
    # exactly: FEATURE-MAP flags the unlocked dict (D6), never this keying.
    return f"{_KEY_PREFIX}{group_id}"


async def _bump(group_id: int, window_seconds: int) -> int | None:
    """The shared counter for this group's current fixed window.

    Returns `None`, never a count, when Valkey is unreachable. The caller must
    treat that as "cannot tell" and do nothing — never as "assume over limit".
    A cache outage silencing anti-spam for a while is a far smaller problem
    than a cache outage turning every sticker in every group into a deleted
    message (see docs/contracts/core_stickerspam.md, "cache outage").
    """
    try:
        return await cache.incr_window(_key(group_id), window_seconds)
    except Exception as exc:  # noqa: BLE001 - infra outage must fail open, not raise
        log.warning("stickerspam.cache_failed", group_id=group_id, error=str(exc))
        return None


@router.message(F.sticker, F.chat.type.in_({"group", "supergroup"}))
async def sticker_anti_spam(message: Message) -> None:
    """Warn at exactly the limit, delete every sticker after it.

    No admin exemption and no feature-flag gate — v1's call site
    (`COOKIEBOT.py:179-180`) has neither, and that is preserved as a real,
    observed v1 quirk rather than "fixed": an admin's own sticker flood counts
    and gets deleted the same as anyone else's.
    """
    bot = cast(Bot, message.bot)
    ctx = await context_for(bot, message)
    count = await _bump(ctx.group_id, ctx.config.sticker_spam_window_s)
    if count is None:
        return

    limit = ctx.config.sticker_spam_limit
    if count == limit:
        await message.reply(t(ctx, "flood_stickers"))
    elif count > limit:
        # v1's delete_message swallows its own exception and prints
        # (`universal_funcs.py:340-344`) — a message already gone (deleted by
        # an admin, or the group migrated) must not break the update.
        with contextlib.suppress(Exception):
            await bot.delete_message(message.chat.id, message.message_id)
