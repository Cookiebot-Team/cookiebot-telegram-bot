"""util_birthday — `/birthday` (aliased `/aniversario`, `/aniversário`,
`/cumpleanos`, `/cumpleaños`), the manual "who has a birthday today" collage.

v1: `birthday`, `../COOKIEBOT-Telegram-Group-Bot/Bot/Birthdays.py:14-61`,
dispatched `COOKIEBOT.py:242-243` — always `manual_chat_id=chat_id`, the
manual shape. v1's *other* invocation shape (`manual_chat_id=None`, a
daily, unattended, every-group broadcast) is not built here — see
`docs/contracts/util_birthday.md` for why that absence is an **open parity
gap**, not something confirmed unnecessary.

Contract: `docs/contracts/util_birthday.md`. Design: `.specs/features/util_birthday/`.
QA: `../Cookiebot-QA/features/util_birthday.feature` — and its **recorded
conflict**: the one QA scenario is a bare `/birthday`, expecting a montage.
v1 does not do that — see below.

## A bare `/birthday` does not show today's birthdays

v1's very first check (`Birthdays.py:16-18`) is `if manual_chat_id and
len(msg['text'].split()) == 1: reply bday.title; return` — a bare
`/birthday` (exactly one token, the command itself) **always** hits this
branch and asks the caller to type usernames; it never looks up who
actually has a birthday. Only `/birthday <anything else>` — a second token,
`@`-prefixed or not — reaches the real lookup. This handler preserves that
exactly (AGENTS.md: v1 code wins for observable behaviour); the QA
conflict is recorded in `docs/contracts/util_birthday.md` and
`docs/site/content/docs/feature-map.mdx`, not silently resolved either way.

## What moves to cb-worker, and why

Real photo compositing (fetch photos, build a grid, overlay confetti) is
exactly AGENTS.md §2.4's "image compositing" case — this handler only does
the free, synchronous parts (the gate, the bare-argument check) and enqueues
`jobs.BIRTHDAY_COLLAGE` with scalars, same discipline `util_everyone`/
`util_calladms`/`util_youtube` already established.
"""

from __future__ import annotations

from typing import cast

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.types import Message

from cb_core import birthdays, jobs
from cb_core.textmatch import ParsedCommand
from cb_gateway.context import context_for, t
from cb_gateway.filters import CommandName
from cb_gateway.queue import enqueue
from cb_gateway.telemetry import mark_outcome

router = Router(name="birthday")


@router.message(F.chat.type != ChatType.PRIVATE, CommandName("birthday"))
async def birthday(message: Message, parsed: ParsedCommand | None = None) -> None:
    """`/birthday` and its aliases. See the module docstring for the recorded
    QA conflict on the bare-argument case."""
    if parsed is None:  # pragma: no cover - CommandName always injects a match
        return

    ctx = await context_for(cast(Bot, message.bot), message)
    if not ctx.enabled("fun"):
        mark_outcome("refused")
        await message.reply(t(ctx, "fun_off"))
        return

    if not parsed.args.strip():
        # v1: len(msg['text'].split()) == 1 -- no lookup at all (`:16-18`).
        await message.reply(birthdays.bday_title(ctx.lang))
        return

    extra_names = [token for token in parsed.args.split() if token.startswith("@")]
    await enqueue(
        jobs.BIRTHDAY_COLLAGE,
        group_id=ctx.group_id,
        message_id=message.message_id,
        extra_names=extra_names,
        lang=ctx.lang,
    )


__all__ = ["birthday", "router"]
