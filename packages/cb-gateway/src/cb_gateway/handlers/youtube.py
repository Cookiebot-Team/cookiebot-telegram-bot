"""util_youtube — `/youtube <query>` searches YouTube and posts a random pick.

v1: `youtube_search`, `../COOKIEBOT-Telegram-Group-Bot/Bot/SocialContent.py:172-189`,
dispatched `COOKIEBOT.py:248-249,260-261` under the `functionsUtility` gate —
the second `elif` chain (`utilityfunctions`), not the `funfunctions` one, same
distinction `dice.py`'s docstring already makes for its own gate.

Contract: `docs/contracts/util_youtube.md`. Design: `.specs/features/util_youtube/`.
QA: `../Cookiebot-QA/features/util_youtube.feature`.

Only the gate and the no-query check stay here — both free, synchronous,
matching v1's own order (`:173-176` runs before the API call). The search
itself is an external API call with no v1 timeout at all
(`googleapiclient`'s bare default), exactly AGENTS.md §2.4's "nothing slow on
the reply path" case, so it and the eventual reply move to `cb-worker`
(`cb_worker/jobs/youtube.py`) behind the same `enqueue` wiring
`util_everyone`/`util_calladms` already established — third consumer.
"""

from __future__ import annotations

from typing import cast

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.types import Message

from cb_core import jobs
from cb_core.textmatch import ParsedCommand
from cb_gateway.context import context_for, t
from cb_gateway.filters import CommandName
from cb_gateway.queue import enqueue
from cb_gateway.telemetry import mark_outcome

router = Router(name="youtube")


@router.message(F.chat.type != ChatType.PRIVATE, CommandName("youtube"))
async def youtube(message: Message, parsed: ParsedCommand | None = None) -> None:
    """`/youtube` (aliased in `cb_core/textmatch.py`). See the module
    docstring for what stays here vs. what moves to `cb-worker`."""
    if parsed is None:  # pragma: no cover - CommandName always injects a match
        return

    ctx = await context_for(cast(Bot, message.bot), message)
    if not ctx.enabled("utility"):
        # v1: notify_utility_off (Miscellaneous.py:133-135) — its send_message
        # call passes `msg` positionally into `msg_to_reply`, i.e. a reply.
        mark_outcome("refused")
        await message.reply(t(ctx, "utility_off"))
        return

    query = parsed.args.strip()
    if not query:
        # v1: len(msg['text'].split()) == 1, no API call at all (`:173-176`).
        await message.reply(t(ctx, "youtube_need"))
        return

    await enqueue(
        jobs.YOUTUBE_SEARCH,
        group_id=ctx.group_id,
        message_id=message.message_id,
        query=query,
        lang=ctx.lang,
    )


__all__ = ["router", "youtube"]
