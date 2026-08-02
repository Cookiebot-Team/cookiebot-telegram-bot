"""util_nextbirthday — `/nextbirthday` (aliased `/proximosaniversarios`,
`/nextbirthdays`, `/proximoscumpleanos`), the upcoming-birthdays list.

v1: `next_birthdays`, `../COOKIEBOT-Telegram-Group-Bot/Bot/Birthdays.py:104-117`,
dispatched `COOKIEBOT.py:244-245`. No image, no external API — four
single-shard-equivalent reads and one text reply, fast enough to stay on the
reply path (unlike `/birthday`'s collage). v1's own `next_birthdays` never
does anything slow either.

Contract: `docs/contracts/util_nextbirthday.md`. QA:
`../Cookiebot-QA/features/util_nextbirthday.feature`, no conflict.

`cb_core.birthdays.next_birthdays_text` is shared with
`cb_worker.jobs.birthday.next_birthdays_followup` — the durable replacement
for v1's `threading.Timer(900, next_birthdays, ...)` follow-up
(`docs/contracts/util_birthday.md`'s D-BD-2) — so both render identically,
matching v1's own reuse of one function from two call sites.

**Not group-scoped, matching v1 exactly**: `next_birthdays_text` reads
`cb_core.birthdays.all_users_with_birthday`, which is deliberately
unfiltered by group — v1's `next_birthdays` does the same (see that
function's own docstring for the evidence). This is a genuine, confirmed
difference from `/birthday`'s own collage, which *is* filtered to the
invoking group.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.types import Message

from cb_core import birthdays
from cb_gateway.context import context_for, t
from cb_gateway.filters import CommandName
from cb_gateway.telemetry import mark_outcome

router = Router(name="nextbirthday")


@router.message(F.chat.type != ChatType.PRIVATE, CommandName("nextbirthday"))
async def next_birthday(message: Message) -> None:
    ctx = await context_for(cast(Bot, message.bot), message)
    if not ctx.enabled("fun"):
        mark_outcome("refused")
        await message.reply(t(ctx, "fun_off"))
        return

    today = datetime.now(UTC).date()
    text = await birthdays.next_birthdays_text(ctx.lang, today)
    # v1: `send_message(cookiebot, chat_id, text)` (`Birthdays.py:112`) -- no
    # `msg_to_reply` argument at all, unlike `/birthday`'s collage. A new
    # message, not a reply.
    await message.answer(text)


__all__ = ["next_birthday", "router"]
