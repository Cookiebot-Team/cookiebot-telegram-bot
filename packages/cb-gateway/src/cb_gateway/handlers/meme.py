"""fun_meme — `/meme` pastes members' faces into a meme template.

v1: `meme`, `../COOKIEBOT-Telegram-Group-Bot/Bot/SocialContent.py:224-277`,
dispatched `COOKIEBOT.py:214,222-223` under the `funfunctions` gate.
Contract: `docs/contracts/fun_meme.md`. Spec: `.specs/features/fun_meme/spec.md`.
No QA scenario exists — `qa/features/fun_meme.feature` is authored, not ported.

Only the two free, synchronous decisions stay here, in v1's own order: the fun
gate, and the "more than five tagged members" refusal (`:230-233`, which v1
also makes before touching anything). The template fetch, the profile-photo
downloads and the compositing pass are `cb_worker/jobs/meme.py` — image
compositing is worker work (AGENTS.md §2.4, and `scripts/spec.py`'s own note on
this feature).

Tag parsing is `fun_battle`'s `parse_tagged_targets`, imported rather than
copied: both features are calling v1's single `get_members_tagged`
(`SocialContent.py:104-111`), warts included, and two transcriptions of one v1
function would eventually disagree.
"""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.types import Message

from cb_core import jobs
from cb_core.logging import get_logger
from cb_gateway.context import context_for, deny_if_disabled, t
from cb_gateway.filters import CommandName
from cb_gateway.handlers.battle import parse_tagged_targets
from cb_gateway.queue import enqueue
from cb_gateway.telemetry import mark_outcome

log = get_logger("cb.meme")

router = Router(name="meme")

#: v1: `if len(members_tagged) > 5` (`:230`). Five is also the largest
#: `blob_count` any template has (`cb_core.meme_templates.MAX_BLOBS`).
MAX_TAGGED = 5


@router.message(F.chat.type != ChatType.PRIVATE, CommandName("meme"))
async def meme(message: Message, bot: Bot) -> None:
    """`/meme`. v1 has no alias for it — one spelling in the dispatcher."""
    ctx = await context_for(bot, message)
    if await deny_if_disabled(message, ctx, "fun"):
        return

    tagged = parse_tagged_targets(message.text or "")
    if len(tagged) > MAX_TAGGED:
        mark_outcome("refused")
        await message.reply(t(ctx, "meme_no"))
        return

    # v1 sends this before it starts fetching (`:226`); the fetching now
    # happens elsewhere, but the group still sees the same "working on it".
    await bot.send_chat_action(ctx.group_id, "upload_photo")

    await enqueue(
        jobs.COMPOSE_MEME,
        group_id=ctx.group_id,
        message_id=message.message_id,
        tagged=tagged,
        lang=ctx.lang,
    )


__all__ = ["MAX_TAGGED", "meme", "router"]
