"""core_privacy — `/privacy` shows the bot's privacy statement.

v1: `privacy_statement`, `../COOKIEBOT-Telegram-Group-Bot/Bot/Miscellaneous.py:60-63`,
dispatched from two places in `COOKIEBOT.py`: the group branch
(`:195-196`, for `/privacy`, `/privacidade`, `/privacidad`, already in
`cb_core/textmatch.py:COMMAND_ALIASES`) and the private-chat branch
(`:87-88`, `privacy_statement(cookiebot, msg, chat_id, 'eng')` — hardcoded
English, regardless of the sender's own language).

QA: `Cookiebot-QA/features/core_privacy.feature`.
Contract: `docs/contracts/core_privacy.md`.

No admin gate, no feature-flag gate, no persistence in either chat kind — v1
answers unconditionally regardless of `functionsFun`/`functionsUtility`, and
so does this handler.

The two branches are two separate, chat-type-filtered handlers rather than
one with a branch inside it: `privacy_private` never calls `context_for`,
which used to be this file's live bug — a DM `/privacy` fell through to the
one handler below, which reads `group_id = message.chat.id` and queries
`group_configs` (distributed on `group_id`) with a private chat's own id, a
"group" that never existed. See `.specs/features/private_dispatch/spec.md`
("The live bug") and `cb_gateway/private_context.py`'s module docstring for
the full reasoning; `docs/contracts/core_listcommand.md`'s handler already
established the fix for `/commands`, avoiding `context_for` entirely for a
DM rather than trying to make it safe for one.
"""

from __future__ import annotations

from typing import cast

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.types import Message

from cb_core import locales
from cb_gateway.context import context_for, t
from cb_gateway.filters import CommandName

router = Router(name="privacy")


@router.message(F.chat.type == ChatType.PRIVATE, CommandName("privacy"))
async def privacy_private(message: Message) -> None:
    """v1: the private-chat branch, `COOKIEBOT.py:87-88` — hardcoded English,
    no group to look a language up for."""
    await message.reply(locales.get("privacy", "en"))


@router.message(F.chat.type != ChatType.PRIVATE, CommandName("privacy"))
async def privacy(message: Message) -> None:
    ctx = await context_for(cast(Bot, message.bot), message)
    await message.reply(t(ctx, "privacy"))


__all__ = ["privacy", "privacy_private", "router"]
