"""core_musicdetection — every voice note is checked against Shazam.

v1: `identify_music`, `../COOKIEBOT-Telegram-Group-Bot/Bot/Audio.py:6-20`,
dispatched from the `voice` content-type branch, `COOKIEBOT.py:155-159`.
Contract: `docs/contracts/core_musicdetection.md`. Spec:
`.specs/features/core_musicdetection/spec.md`. No QA scenario exists for this
feature — `qa/features/core_musicdetection.feature` is authored, not ported.

**This handler must never consume the update.** v1's `voice` branch is *not*
an `if/elif` chain over the two things it can do: it runs the music check
under `utilityfunctions` and then, in the same branch, the transcribe→AI
sub-step under `funfunctions` (`COOKIEBOT.py:156-162`). Both fire for the same
voice note. aiogram stops at the first router that handles an update, so this
one raises `SkipHandler` on every path — including the one where it enqueued —
and `transcribe.router` downstream still sees the note. Registered *before*
`transcribe` for the same reason v1 does the music check first: it is the
branch that runs unconditionally, and the AI one has an extra precondition
(the note must reply to the bot).

Everything expensive is `cb_worker/jobs/music.py`: an unofficial external API
on the highest-volume path in the bot (AGENTS.md §2.4, and FEATURE-MAP §5's
account of what that cost v1).
"""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.enums import ChatType
from aiogram.types import Message

from cb_core import jobs
from cb_core.logging import get_logger
from cb_core.settings import get_settings
from cb_gateway.context import context_for
from cb_gateway.queue import enqueue

log = get_logger("cb.musicdetection")

router = Router(name="musicdetection")


@router.message(F.chat.type != ChatType.PRIVATE, F.voice)
async def identify_music(message: Message, bot: Bot) -> None:
    """Hand a voice note to the fingerprinting job, then yield.

    Gated twice: `CB_MUSIC_DETECTION_ENABLED` (off by default — the feature
    calls an unofficial API, see `cb_worker/music.py`) and then v1's own
    `functionsUtility`. Neither refusal answers anything: v1 dispatches this
    from a bare `if utilityfunctions:` with no `else`, exactly like the link
    embedder, because a voice note never asked for anything.
    """
    if not get_settings().music_detection_enabled:
        raise SkipHandler

    ctx = await context_for(bot, message)
    if not ctx.enabled("utility"):
        raise SkipHandler

    if message.voice is not None:
        await enqueue(
            jobs.IDENTIFY_MUSIC,
            group_id=ctx.group_id,
            message_id=message.message_id,
            file_id=message.voice.file_id,
            lang=ctx.lang,
        )

    # Always — see the module docstring. The transcribe→AI sub-step downstream
    # runs for the same note in v1, and swallowing the update here would
    # silently disable it.
    raise SkipHandler


__all__ = ["identify_music", "router"]
