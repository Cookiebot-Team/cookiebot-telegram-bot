"""x_distortion — `/destroy` (aliased `/zoar`, `/destruir`) mangles a reply.

v1: `destroy`, `../COOKIEBOT-Telegram-Group-Bot/Bot/Miscellaneous.py:377-433`,
dispatched `COOKIEBOT.py:216-217,242-243` under the `funfunctions` gate.
Contract: `docs/contracts/x_distortion.md`. Spec:
`.specs/features/x_distortion/spec.md`. No QA scenario exists for this feature
— `qa/features/x_distortion.feature` is authored, not ported.

This module is v1's branch chain and nothing else. Every branch is a free,
synchronous decision about what the message replied to, made in v1's own
order, and each one either answers immediately or enqueues
`jobs.DISTORT_MEDIA`. The download, the seam carve and the ffmpeg pass are
`cb_worker/jobs/distortion.py` — see that module for D3/D4 and why they are
not on the reply path.

**Video and GIF distortion are switched off in v1 and stay off.** `destroy`
answers `destroy.video` for a video (`:394-396`) and `destroy.gif` for an
animation or an animated/video sticker (`:418-420,428-430`), and the frame
pipeline behind those strings is unreachable from the bot. Reporting them as
"currently disabled" is the ported behaviour, not a v2 shortcut.

One branch differs from v1, and only because v1's raises. `/destroy pfp` for
someone with no public profile photo indexes `['photos'][0]` on an empty list
(`:382`), which dies in v1's top-level handler with no reply at all. Here it
answers `battle_no_picture` — v1's own "you need a profile picture (or it's
private)" string, already reused for exactly this case by `fun_battle`'s port.
"""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.types import Message

from cb_core import jobs, locales
from cb_core.logging import get_logger
from cb_core.textmatch import ParsedCommand
from cb_gateway.context import ChatContext, context_for, deny_if_disabled, t
from cb_gateway.filters import CommandName
from cb_gateway.queue import enqueue

log = get_logger("cb.destroy")

router = Router(name="destroy")

#: v1 keeps this feature's three strings in a nested `destroy` object.
_SECTION = "destroy"

#: What `_resolve` decided the command should do.
REFUSE_INSTRUCTIONS = "instru"
REFUSE_VIDEO = "video"
REFUSE_GIF = "gif"


def wants_own_photo(text: str) -> bool:
    """v1: `msg['text'].endswith('pfp')` (`:379`) — the whole message, not an
    argument, so `/destroy my pfp` also matches. Preserved."""
    return text.endswith("pfp")


def resolve_reply(replied: Message) -> tuple[str, str | None]:
    """v1's `elif` chain over the replied-to message (`:393-433`).

    Returns `(kind, file_id)` for something distortable, or
    `(refusal, None)` where the refusal is one of the three `destroy.*` keys.
    The order is v1's exactly: video, photo, audio/voice, sticker, animation,
    then the trailing `else`.
    """
    if replied.video is not None:
        return REFUSE_VIDEO, None
    if replied.photo:
        # v1 asks `get_media_content` for `'photo'`, which resolves the largest
        # size (`universal_funcs.py`'s `msg['photo'][-1]`).
        return "photo", replied.photo[-1].file_id
    if replied.audio is not None:
        return "audio", replied.audio.file_id
    if replied.voice is not None:
        return "audio", replied.voice.file_id
    if replied.sticker is not None:
        sticker = replied.sticker
        if sticker.is_animated or sticker.is_video:
            return REFUSE_GIF, None
        return "sticker", sticker.file_id
    if replied.animation is not None:
        return REFUSE_GIF, None
    return REFUSE_INSTRUCTIONS, None


def _refusal(ctx: ChatContext, key: str) -> str:
    return locales.get_nested(_SECTION, key, ctx.lang)


@router.message(F.chat.type != ChatType.PRIVATE, CommandName("destroy"))
async def destroy(message: Message, bot: Bot, parsed: ParsedCommand | None = None) -> None:
    """`/destroy`, `/zoar`, `/destruir` (aliased in `cb_core/textmatch.py`)."""
    ctx = await context_for(bot, message)
    if await deny_if_disabled(message, ctx, "fun"):
        return

    if wants_own_photo(parsed.raw if parsed is not None else (message.text or "")):
        await _destroy_own_photo(message, bot, ctx)
        return

    replied = message.reply_to_message
    if replied is None:
        # v1's second branch (`:393-394`) and its trailing `else` (`:432-433`)
        # send the same instruction string.
        await message.reply(_refusal(ctx, REFUSE_INSTRUCTIONS))
        return

    kind, file_id = resolve_reply(replied)
    if file_id is None:
        await message.reply(_refusal(ctx, kind))
        return

    await bot.send_chat_action(ctx.group_id, "upload_voice" if kind == "audio" else "upload_photo")
    await enqueue(
        jobs.DISTORT_MEDIA,
        group_id=ctx.group_id,
        message_id=message.message_id,
        file_id=file_id,
        kind=kind,
        lang=ctx.lang,
    )


async def _destroy_own_photo(message: Message, bot: Bot, ctx: ChatContext) -> None:
    """v1's `pfp` branch (`:379-392`), minus the crash on an empty list.

    v1 also downloads the file itself here, through a URL it builds with the
    bot token in it (`:383`) — the same construction `x_reverse_search`'s port
    removed (D-RS-1). Nothing here builds a URL: the `file_id` goes on the
    queue and the worker downloads through the Bot API session.
    """
    if message.from_user is None:  # pragma: no cover - a channel post has no sender
        await message.reply(_refusal(ctx, REFUSE_INSTRUCTIONS))
        return

    await bot.send_chat_action(ctx.group_id, "upload_photo")
    photos = await bot.get_user_profile_photos(message.from_user.id, limit=1)
    if not photos.photos:
        await message.reply(t(ctx, "battle_no_picture"))
        return

    await enqueue(
        jobs.DISTORT_MEDIA,
        group_id=ctx.group_id,
        message_id=message.message_id,
        file_id=photos.photos[0][-1].file_id,
        kind="photo",
        lang=ctx.lang,
    )


__all__ = [
    "REFUSE_GIF",
    "REFUSE_INSTRUCTIONS",
    "REFUSE_VIDEO",
    "destroy",
    "resolve_reply",
    "router",
    "wants_own_photo",
]
