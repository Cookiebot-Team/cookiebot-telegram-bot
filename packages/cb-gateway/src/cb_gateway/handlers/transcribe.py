"""x_speech_to_text -- a voice note becomes text, two ways.

v1: `Bot/Audio.py:22-32` (`speech_to_text`), called only from the reply-to-bot
voice branch at `Bot/COOKIEBOT.py:155-162`. Read `.specs/features/
x_speech_to_text/spec.md` and `design.md` first -- every behavioural decision
here (R1, R2, R6) is settled there, alongside `.specs/features/
x_conversational_ai/design.md`, whose `reply_with_ai` (R5.9) shape (a) calls
directly.

Two shapes, one module (design.md's "Module placement" table):

- Shape (a), `voice_ai`: the ported sub-step. A voice note replying to this
  bot is transcribed and the transcript is fed straight into
  `chat_ai.reply_with_ai` -- v1's own structure (`COOKIEBOT.py:161-162`
  assigns the transcript to `msg['text']` and calls the same function text
  triggers use). The transcript itself is never shown -- v1 parity.
- Shape (b), `/transcribe`: net-new. Reply to any voice note with the
  trigger and get the transcript back, gated on `utility` with the standard
  notice -- deliberately unlike (a), which stays silent when its gate
  (`fun`) is closed, matching v1's own silence on that path.

Both share the duration cap (D-ST-3, checked before anything is downloaded),
the no-disk download (D-ST-1 -- already impossible by construction:
`bot.download` returns an in-memory buffer and `router().transcribe` takes
`bytes`, never a path), the language hint (D-ST-5) and `transcribe_failed` as
the catch-all failure reply (D-ST-6, covering an `LLMError`, a timeout or a
failed download alike -- design.md R5).
"""

from __future__ import annotations

from typing import cast

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import BaseFilter
from aiogram.types import Message, Voice
from prometheus_client import Counter

from cb_core import cache, tenancy
from cb_core.llm import router as llm_router
from cb_core.llm.types import Transcript
from cb_core.logging import get_logger
from cb_core.settings import get_settings
from cb_core.textmatch import ParsedCommand
from cb_gateway.context import ChatContext, context_for, deny_if_disabled, t
from cb_gateway.filters import CommandName, FeatureGate
from cb_gateway.handlers.chat_ai import reply_with_ai
from cb_gateway.telemetry import mark_outcome

log = get_logger("cb.transcribe")

router = Router(name="transcribe")

# R6: `shape` in {"voice_ai", "command"}, `outcome` in {"ok", "too_long",
# "no_voice", "error"} -- eight combinations, never group_id/user_id
# (AGENTS.md SS7's cardinality rule). "no_voice" only ever fires for
# "command": shape (a) is dispatched by `F.voice` in the first place.
transcribe_total = Counter(
    "cb_gateway_transcribe_total",
    "Outcomes of a voice-note transcription, by trigger shape",
    ["shape", "outcome"],
)

# R2.6: Telegram caps a message at 4096 characters; truncate to 4000 + "..."
# rather than splitting into a thread of continuations (design.md R7.3).
_MAX_REPLY_CHARS = 4000


# ------------------------------------------------------------------------- I/O


async def _download(bot: Bot, file_id: str) -> bytes | None:
    """The exact `bot.download(file_id)` idiom `fun_random.py:177-186`'s own
    `_download` uses. A failure here (no file access, a timeout, a mock
    Telegram that has no `getFile`) must never take the update down with
    it -- that is the single seam callers wrap nothing further around.
    """
    try:
        buffer = await bot.download(file_id)
    except Exception as exc:  # noqa: BLE001 - downloading is best-effort, see above
        log.warning("transcribe.download_failed", error=str(exc))
        return None
    if buffer is None:
        return None
    return buffer.read()


# ------------------------------------------------------------- reply-to-bot gate


def _is_reply_to_bot(message: Message, bot: Bot) -> bool:
    """R1.1: v1's trigger requires the voice note to reply to one of *this*
    bot's own messages (`COOKIEBOT.py:155,160`).

    Deliberately not `chat_ai._bot_reply_text` reused as a boolean: that
    helper returns `None` both when the reply target is not this bot *and*
    when it is this bot but the target message has no `.text` (a photo,
    say) -- the second case would wrongly read as "not a reply to the bot"
    here. This only cares who sent the reply target, never what it said.
    """
    reply = message.reply_to_message
    return reply is not None and reply.from_user is not None and reply.from_user.id == bot.id


class ReplyToBotFilter(BaseFilter):
    """The aiogram filter wrapping `_is_reply_to_bot` for router
    registration. A voice note that fails this never reaches `voice_ai` at
    all and falls through untouched -- v1 hands it only to `identify_music`
    (`core_musicdetection`, out of scope here)."""

    async def __call__(self, message: Message, bot: Bot | None = None) -> bool:
        return _is_reply_to_bot(message, cast(Bot, bot or message.bot))


# --------------------------------------------------------------- group window


def _group_window_key(group_id: int) -> str:
    """R1.7: deliberately the *same* key format `chat_ai.py`'s own (private)
    `_group_key` builds (`f"cb:ai:{group_id}"`), not a parallel counter.
    R1.7 requires the per-group AI-reply rate limit to bind the voice path
    too -- a distinct counter here would just be a second, independently
    refillable allowance, letting a group double its effective AI-reply
    rate by alternating text mentions and voice replies instead of being
    capped by the one limit both are meant to share. Duplicated rather than
    imported because it is one private, `_`-prefixed line in another
    handler module, not a shared export.
    """
    return f"cb:ai:{group_id}"


async def _bump_group_window(group_id: int, window_seconds: int) -> int | None:
    """Same fail-open contract as `chat_ai._bump_group` / `stickerspam._bump`:
    `None` means "cannot tell", never "assume over the limit"."""
    try:
        return await cache.incr_window(_group_window_key(group_id), window_seconds)
    except Exception as exc:  # noqa: BLE001 - infra outage must fail open, not raise
        log.warning("transcribe.group_window_failed", group_id=group_id, error=str(exc))
        return None


# ------------------------------------------------------------- shared pipeline


async def _get_transcript(
    message: Message,
    ctx: ChatContext,
    *,
    bot: Bot,
    skin: str,
    voice: Voice,
    shape: str,
    reply_target: Message,
) -> Transcript | None:
    """The duration cap (R1.3/R2.4/D-ST-3, checked before any download), the
    no-disk download (R1.4/R2.4/D-ST-1) and the bounded, language-hinted
    `transcribe` call (R1.5/R2.4/D-ST-5). Every failure path replies via
    `reply_target` and marks the outcome (D-ST-6, R5) so callers only need
    to check the return value for `None`.
    """
    settings = get_settings()
    if voice.duration > settings.transcribe_max_duration_seconds:
        transcribe_total.labels(shape=shape, outcome="too_long").inc()
        mark_outcome("refused")
        await reply_target.reply(
            t(ctx, "transcribe_too_long", max=settings.transcribe_max_duration_seconds)
        )
        return None

    audio = await _download(bot, voice.file_id)
    if audio is None:
        transcribe_total.labels(shape=shape, outcome="error").inc()
        mark_outcome("refused")
        await reply_target.reply(t(ctx, "transcribe_failed"))
        return None

    tenant = await tenancy.registry.by_skin(skin)
    user_id = message.from_user.id if message.from_user is not None else None
    try:
        transcript = await llm_router().transcribe(
            audio,
            filename="voice.ogg",
            language=ctx.lang,
            group_id=ctx.group_id,
            user_id=user_id,
            tenant_id=tenant.tenant_id,
        )
    except Exception as exc:  # noqa: BLE001 - D-ST-6: every failure path still replies
        log.warning("transcribe.failed", shape=shape, error=str(exc))
        transcribe_total.labels(shape=shape, outcome="error").inc()
        mark_outcome("refused")
        await reply_target.reply(t(ctx, "transcribe_failed"))
        return None

    transcribe_total.labels(shape=shape, outcome="ok").inc()
    return transcript


# --------------------------------------------------------------------- handlers


@router.message(
    F.chat.type != ChatType.PRIVATE,
    F.voice,
    FeatureGate("fun"),
    ReplyToBotFilter(),
)
async def voice_ai(
    message: Message,
    bot: Bot | None = None,
    skin: str = tenancy.DEFAULT_TENANT,
    bot_username: str = "",
) -> None:
    """R1: v1's only call site for speech-to-text (`COOKIEBOT.py:155-162`) --
    a voice note replying to this bot is transcribed and fed straight into
    `chat_ai.reply_with_ai`.

    `FeatureGate("fun")`, not `deny_if_disabled` (R1.2): a closed gate
    declines silently, same as v1 sends no `fun_off` notice on this path
    either (`COOKIEBOT.py:160`, contrast the command-gate notices in shape
    (b) below).
    """
    voice = message.voice
    if voice is None:  # pragma: no cover - F.voice guarantees this
        return

    active_bot = cast(Bot, bot or message.bot)
    ctx = await context_for(active_bot, message)
    settings = get_settings()

    # R1.7: the per-group AI-reply rate limit applies to this path too, even
    # though the per-user streak (R4) deliberately does not -- see
    # `_group_window_key`'s docstring. Mirrors chat_ai.ai_reply's own
    # gate-order and reply-once-at-the-limit behaviour exactly, since the
    # two paths now share the one counter.
    group_count = await _bump_group_window(ctx.group_id, settings.ai_chat_window_seconds)
    if group_count is not None and group_count >= settings.ai_chat_group_limit:
        if group_count == settings.ai_chat_group_limit:
            await message.reply(t(ctx, "ai_rate_limited"))
            mark_outcome("refused")
        else:
            mark_outcome("silent")
        return

    transcript = await _get_transcript(
        message,
        ctx,
        bot=active_bot,
        skin=skin,
        voice=voice,
        shape="voice_ai",
        reply_target=message,
    )
    if transcript is None:
        return

    # R1.6/D-ST-4: the transcript is never shown -- v1 parity
    # (`COOKIEBOT.py:161` assigns it to `msg['text']` and nothing else reads
    # it). No `.capitalize()`. The per-user streak stays untouched:
    # `reply_with_ai` itself never spends it (chat_ai.py's own R4.5 note).
    await reply_with_ai(message, ctx, skin=skin, bot_username=bot_username, text=transcript.text)


@router.message(CommandName("transcribe"))
async def transcribe_command(
    message: Message,
    parsed: ParsedCommand | None = None,
    skin: str = tenancy.DEFAULT_TENANT,
) -> None:
    """`/transcribe` (aliased `/transcrever`, `/transcribir` in
    `cb_core/textmatch.py`) -- R2, net-new, no v1 equivalent.

    Gated on `utility` (a transcript is a utility, not a bit) with
    `deny_if_disabled`'s standard notice -- deliberately unlike shape (a)'s
    silent `fun` gate (R2.2).
    """
    if parsed is None:  # pragma: no cover - CommandName always injects a match
        return

    active_bot = cast(Bot, message.bot)
    ctx = await context_for(active_bot, message)
    if await deny_if_disabled(message, ctx, "utility"):
        mark_outcome("refused")
        return

    reply = message.reply_to_message
    voice = reply.voice if reply is not None else None
    if voice is None or reply is None:
        # R2.3: anything that is not a reply to a voice note says so, rather
        # than staying silent.
        transcribe_total.labels(shape="command", outcome="no_voice").inc()
        mark_outcome("refused")
        await message.reply(t(ctx, "transcribe_no_voice"))
        return

    await active_bot.send_chat_action(message.chat.id, "typing")  # R2.7

    transcript = await _get_transcript(
        message,
        ctx,
        bot=active_bot,
        skin=skin,
        voice=voice,
        shape="command",
        reply_target=message,
    )
    if transcript is None:
        return

    # R2.5/R2.6: the transcript replies to the *voice note*, not the
    # command -- it belongs next to the audio it transcribes -- truncated to
    # Telegram's message cap.
    text = transcript.text
    if len(text) > _MAX_REPLY_CHARS:
        text = text[:_MAX_REPLY_CHARS] + "…"
    await reply.reply(text)
    mark_outcome("answered")


__all__ = [
    "ReplyToBotFilter",
    "router",
    "transcribe_command",
    "transcribe_total",
    "voice_ai",
]
