"""x_reverse_search — `/buscarfonte` finds an image's source.

v1: `reverse_search`, `../COOKIEBOT-Telegram-Group-Bot/Bot/SocialContent.py:113-142`,
dispatched `COOKIEBOT.py:212-213`. Aliases `buscarfonte`/`searchsource`/
`buscarfuente` -> `searchsource`, added to `COMMAND_ALIASES` by this port —
none of the three existed there before.

Spec/design: `.specs/features/x_reverse_search/`. Contract:
`docs/contracts/x_reverse_search.md`.

The gateway keeps only what is free and synchronous, matching v1's own order:
the `functionsUtility` gate, the reply requirement, and resolving which file to
search. The SauceNAO call is `cb_worker/jobs/reverse_search.py` — an unbounded
external call (AGENTS.md §2.4), and the place where v1 leaks the bot token to a
third party (spec D-RS-1, fixed there).
"""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.types import Message

from cb_core import jobs
from cb_core.logging import get_logger
from cb_gateway.context import context_for, deny_if_disabled, t
from cb_gateway.filters import CommandName
from cb_gateway.queue import enqueue

log = get_logger("cb.reverse_search")

router = Router(name="reverse_search")


def file_id_of(message: Message) -> str | None:
    """Which file v1's `fetch_temp_jpg` would have fetched (`:86-98`).

    Largest photo size first, then `document` — v1 expresses that as a
    `try`/`except KeyError` around `msg['photo']`, which means a reply carrying
    *neither* raises a second `KeyError` on `msg['document']` and kills the
    update with no reply at all (spec D-RS-5). An explicit check instead; the
    caller answers with the same string the no-reply branch uses, because v1
    has no separate one and this port does not invent one.

    v1 also accepts a `document` that is a *video* and pulls a frame out of it
    with OpenCV (`:98-102`) — but only on the `only_return_url=False` path,
    which this feature never takes. The URL path hands the document straight to
    SauceNAO whatever it is, so this does too.
    """
    if message.photo:
        return message.photo[-1].file_id
    if message.document:
        return message.document.file_id
    return None


@router.message(CommandName("searchsource"), F.chat.type != ChatType.PRIVATE)
async def search_source(message: Message, bot: Bot) -> None:
    """v1's `reverse_search` (`:113-142`), minus everything that blocks.

    Gated on `functionsUtility` only — no admin check and no cooldown; v1 has
    neither, and `Cooldowns.py` has no entry for this command.
    """
    ctx = await context_for(bot, message)
    if await deny_if_disabled(message, ctx, "utility"):
        return

    replied = message.reply_to_message
    if replied is None:
        await message.reply(t(ctx, "reverse_image"))
        return

    file_id = file_id_of(replied)
    if file_id is None:
        await message.reply(t(ctx, "reverse_image"))
        return

    await enqueue(
        jobs.REVERSE_SEARCH,
        group_id=message.chat.id,
        message_id=message.message_id,
        file_id=file_id,
        lang=ctx.lang,
    )


__all__ = ["file_id_of", "router"]
