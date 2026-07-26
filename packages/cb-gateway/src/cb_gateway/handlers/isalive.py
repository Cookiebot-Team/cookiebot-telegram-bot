"""util_isalive — health check from inside a chat.

QA: Cookiebot-QA/features/util_isalive.feature
  - bot running      -> confirms it is alive and operational
  - bot not running  -> no response (nothing to implement; asserted by the harness)

M0's acceptance gate: this is the one handler that proves the whole path —
webhook -> dedupe -> telemetry -> parser -> handler -> Telegram API.
"""

from __future__ import annotations

import time

from aiogram import Router
from aiogram.types import Message

from cb_core.cooldowns import COMPILED
from cb_gateway.filters import CommandName

router = Router(name="isalive")

_STARTED = time.monotonic()


def _uptime() -> str:
    seconds = int(time.monotonic() - _STARTED)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


@router.message(CommandName("isalive"))
async def isalive(message: Message) -> None:
    await message.reply(
        "🍪 <b>Alive and operational.</b>\n"
        f"uptime <code>{_uptime()}</code> · "
        f"build <code>0.1.0</code> · "
        f"hot path <code>{'cython' if COMPILED else 'pure-python'}</code>"
    )
