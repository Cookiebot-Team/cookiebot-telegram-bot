"""core_privacy — `/privacy` shows the bot's privacy statement.

v1: `privacy_statement`, `../COOKIEBOT-Telegram-Group-Bot/Bot/Miscellaneous.py:60-63`,
dispatched from `COOKIEBOT.py:195-196` for `/privacy`, `/privacidade`,
`/privacidad` (already in `cb_core/textmatch.py:COMMAND_ALIASES`).

QA: `Cookiebot-QA/features/core_privacy.feature`.
Contract: `docs/contracts/core_privacy.md`.

No admin gate, no feature-flag gate, no persistence — v1 answers unconditionally
regardless of `functionsFun`/`functionsUtility`, and so does this handler.
"""

from __future__ import annotations

from typing import cast

from aiogram import Bot, Router
from aiogram.types import Message

from cb_gateway.context import context_for, t
from cb_gateway.filters import CommandName

router = Router(name="privacy")


@router.message(CommandName("privacy"))
async def privacy(message: Message) -> None:
    ctx = await context_for(cast(Bot, message.bot), message)
    await message.reply(t(ctx, "privacy"))
