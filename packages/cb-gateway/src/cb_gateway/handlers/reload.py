"""core_reload — `/reload`, `/recarregar`: drop this group's cached admins and
config and read them again.

v1: `../COOKIEBOT-Telegram-Group-Bot/Bot/COOKIEBOT.py:197-201`::

    elif msg['text'].startswith(("/reload", "/recarregar")):
        get_admins(cookiebot, chat_id, ignorecache=True, is_alternate_bot=is_alternate_bot)
        get_config(cookiebot, chat_id, ignorecache=True, is_alternate_bot=is_alternate_bot)
        text = i18n.get("reload", lang=language)
        send_message(cookiebot, chat_id, text, msg)

Ungated and un-admin-checked, in the same stretch of the chain as `/privacy`
and `/analise` — anyone in the group can ask for it, and this handler keeps
that.

## Why v2 has it at all

v2 does not need a manual refresh the way v1 did: `group_config` and `admins`
are two-tier caches with pub/sub invalidation, so a setting changed through
`/config` is live on every replica immediately, and FEATURE-MAP D6 records
exactly that as the fix for v1's per-process, never-invalidated dicts. The
`platform_group_config` row says as much — "replaces v1's manual /reload (D6)".

Replacing the *reason* for a command is not the same as replacing the command.
`/reload` and `/recarregar` are still advertised in the help text v2 ships
byte-for-byte from v1 (`Cookiebot_functions.txt`, rendered by `/commands`), and
a group that has been told a command exists gets nothing when they type it —
which is worse than either implementing it or removing the line, and dropping a
v1 trigger is what AGENTS.md §2.1 forbids outright.

So it stays, and it does what it says: a real invalidation, not a stub that
replies "reloaded" while doing nothing. On v2 that is now cheap and it is
occasionally genuinely useful — Telegram's own admin list is what `admins`
refreshes from, and a promotion made in Telegram (not through this bot) is not
something any invalidation of ours could have known about.

`admins.refresh` is what `ignorecache=True` meant: it re-reads
`getChatAdministrators` and rewrites both cache tiers plus `group_admins`.
`group_config.invalidate` drops L1 here, clears L2 and publishes to the other
replicas; the next read repopulates from Postgres.
"""

from __future__ import annotations

from typing import cast

from aiogram import Bot, Router
from aiogram.types import Message

from cb_core import admins, group_config
from cb_core.logging import get_logger
from cb_gateway.context import context_for, t
from cb_gateway.filters import CommandName

log = get_logger("cb.gateway.reload")

router = Router(name="reload")


@router.message(CommandName("reload"))
async def reload(message: Message) -> None:
    bot = cast(Bot, message.bot)
    group_id = message.chat.id

    # Order matters only in that the config read below must not repopulate from
    # a cache this is about to clear: invalidate first, then let `context_for`
    # miss and re-read. v1 refreshed admins first for no stated reason and the
    # order has no observable effect there either.
    await group_config.invalidate(group_id)
    try:
        await admins.refresh(bot, group_id)
    except Exception as exc:  # noqa: BLE001 - Telegram may refuse; the config half still happened
        # v1 would have thrown here and answered nothing at all. Answering is
        # the better failure: the caller asked for a refresh, half of it
        # happened, and silence would leave them retyping the command.
        log.warning("reload.admins_refresh_failed", group_id=group_id, error=str(exc))

    ctx = await context_for(bot, message)
    await message.reply(t(ctx, "reload"))


__all__ = ["reload", "router"]
