"""Unit coverage for `cb_gateway.handlers.transcribe` -- both shapes, no
dispatcher, no real Telegram, no database, no real LLM provider.

See `.specs/features/x_speech_to_text/spec.md` and `design.md` for the full
behaviour contract; the defect table (D-ST-1..D-ST-6) is what most of the
classes below are named after. `docs/contracts/` has no page for this feature
yet -- `design.md` is the source of truth until one exists.

Model: `packages/cb-gateway/tests/test_chat_ai.py`'s handler-level section
(module-global monkeypatching, a duck-typed `ChatContext`, a `SimpleNamespace`
fake router) and `packages/cb-gateway/tests/test_fun_random.py`'s
`_download`-wrapping-`bot.download` idiom (`fun_random.py:177-191`, mirrored
verbatim at `transcribe.py:73-86`).

What is NOT here: an end-to-end aiogram dispatch (feeding a real `Update`
through a real `Dispatcher`) -- that belongs to `qa/`, against mock Telegram,
per AGENTS.md SS6. Handlers are called directly, and the one place that
matters whether a filter is actually *wired onto* a handler (the `fun`
`FeatureGate` on `voice_ai`, R1.2) is checked structurally against
`transcribe.router` instead.
"""

from __future__ import annotations

import builtins
import io
import pathlib
import tempfile
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from cb_core.admins import ActorCheck
from cb_core.group_config import GroupConfig
from cb_core.llm.types import LLMError, Transcript
from cb_core.textmatch import ParsedCommand
from cb_gateway.context import ChatContext
from cb_gateway.filters import FeatureGate
from cb_gateway.handlers import transcribe

# --------------------------------------------------------------------- fakes


@dataclass
class _FakeChat:
    id: int = -100


@dataclass
class _FakeUser:
    id: int = 1


@dataclass
class _FakeVoice:
    """Only `.file_id`/`.duration` -- the handful of attributes `transcribe.py`
    actually reads off an aiogram `Voice`."""

    file_id: str = "voice-file-1"
    duration: int = 10


@dataclass
class _FakeMessage:
    """Only the attributes the handlers actually read. Doubles as both the
    incoming command/voice message *and* (for shape (b)) its own
    `reply_to_message` -- `transcribe_command` needs a second one of these
    with its own `.voice` and its own `.reply`."""

    chat: _FakeChat = field(default_factory=_FakeChat)
    from_user: _FakeUser | None = field(default_factory=_FakeUser)
    voice: Any = None
    reply_to_message: Any = None
    bot: Any = None
    reply: Any = field(default_factory=AsyncMock)


@dataclass
class _FakeTenant:
    tenant_id: str = "cookiebot"
    display_name: str = "Cookiebot"


def _bot(*, download: Any = None) -> Any:
    """`bot.download(file_id)` must return something with a sync `.read()`
    -- `_download` (`transcribe.py:73-86`) calls `buffer.read()` on whatever
    comes back, same contract `fun_random._download` relies on."""
    return SimpleNamespace(
        id=999,
        download=download
        if download is not None
        else AsyncMock(return_value=io.BytesIO(b"ogg-bytes")),
        send_chat_action=AsyncMock(),
    )


def _settings(*, max_duration: int = 300, ai_window: int = 60, ai_limit: int = 20) -> Any:
    return SimpleNamespace(
        transcribe_max_duration_seconds=max_duration,
        ai_chat_window_seconds=ai_window,
        ai_chat_group_limit=ai_limit,
    )


def _ctx(
    *, group_id: int = -100, lang: str = "en", fun: bool = True, utility: bool = True
) -> ChatContext:
    config = GroupConfig(group_id=group_id, functions_fun=fun, functions_utility=utility)
    return ChatContext(
        group_id=group_id,
        config=config,
        lang=lang,
        actor=ActorCheck(user_id=1, is_admin=False, anonymous=False),
    )


def _transcript(text: str = "hello there", *, language: str | None = "en") -> Transcript:
    return Transcript(text=text, model="whisper-1", provider="openai", language=language)


def _parsed(name: str = "transcribe") -> ParsedCommand:
    return ParsedCommand(name, "", "", "/transcribe")


def _fake_router(
    transcript: Transcript | None = None, *, side_effect: Exception | None = None
) -> Any:
    call = (
        AsyncMock(side_effect=side_effect)
        if side_effect is not None
        else AsyncMock(return_value=transcript or _transcript())
    )
    return SimpleNamespace(transcribe=call)


def _wire_happy_collaborators(
    monkeypatch: pytest.MonkeyPatch, *, ctx: ChatContext | None = None
) -> None:
    """The full set of module-global collaborators `voice_ai`/
    `transcribe_command` reach for, wired to succeed -- shared by the tests
    that only care about one specific piece of behaviour further down the
    pipeline."""
    monkeypatch.setattr(transcribe, "context_for", AsyncMock(return_value=ctx or _ctx()))
    monkeypatch.setattr(transcribe, "get_settings", lambda: _settings())
    monkeypatch.setattr(transcribe.cache, "incr_window", AsyncMock(return_value=1))
    monkeypatch.setattr(
        transcribe.tenancy.registry, "by_skin", AsyncMock(return_value=_FakeTenant())
    )


# --------------------------------------------------- duration cap (D-ST-3/R1.3)


class TestGetTranscriptDurationCap:
    """R1.3/R2.4/D-ST-3: the cap is checked against `voice.duration` *before*
    anything is downloaded -- the metadata is already in the update, so an
    oversized note costs neither a download nor a transcription. Both shapes
    funnel through this one shared function (`_get_transcript`), so pinning
    it here covers both at once."""

    async def test_over_cap_rejects_before_any_download(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(transcribe, "get_settings", lambda: _settings(max_duration=60))
        download = AsyncMock()
        bot = _bot(download=download)
        message = _FakeMessage()
        ctx = _ctx()

        result = await transcribe._get_transcript(  # noqa: SLF001
            message,
            ctx,
            bot=bot,
            skin="cookiebot",
            voice=_FakeVoice(duration=61),
            shape="voice_ai",
            reply_target=message,
        )

        assert result is None
        download.assert_not_awaited()
        message.reply.assert_awaited_once_with(transcribe.t(ctx, "transcribe_too_long", max=60))

    async def test_exactly_at_the_cap_is_not_over_it_and_proceeds_to_download(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(transcribe, "get_settings", lambda: _settings(max_duration=60))
        monkeypatch.setattr(
            transcribe.tenancy.registry, "by_skin", AsyncMock(return_value=_FakeTenant())
        )
        monkeypatch.setattr(transcribe, "llm_router", lambda: _fake_router())
        download = AsyncMock(return_value=io.BytesIO(b"ogg-bytes"))
        bot = _bot(download=download)
        message = _FakeMessage()
        ctx = _ctx()

        result = await transcribe._get_transcript(  # noqa: SLF001
            message,
            ctx,
            bot=bot,
            skin="cookiebot",
            voice=_FakeVoice(duration=60),
            shape="voice_ai",
            reply_target=message,
        )

        assert result is not None
        download.assert_awaited_once_with("voice-file-1")

    async def test_under_cap_downloads_normally(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(transcribe, "get_settings", lambda: _settings(max_duration=300))
        monkeypatch.setattr(
            transcribe.tenancy.registry, "by_skin", AsyncMock(return_value=_FakeTenant())
        )
        monkeypatch.setattr(transcribe, "llm_router", lambda: _fake_router())
        download = AsyncMock(return_value=io.BytesIO(b"ogg-bytes"))
        bot = _bot(download=download)
        message = _FakeMessage()

        result = await transcribe._get_transcript(  # noqa: SLF001
            message,
            _ctx(),
            bot=bot,
            skin="cookiebot",
            voice=_FakeVoice(duration=5),
            shape="command",
            reply_target=message,
        )

        assert result is not None
        download.assert_awaited_once()


# ---------------------------------------------- no filesystem write (D-ST-1)


def _path_open_spy() -> tuple[Any, list[Any]]:
    """A plain function assigned onto the class binds `self` normally --
    unlike a bare `MagicMock`, which is not a descriptor and would silently
    drop `self` when accessed through an instance, making the "was this
    called" signal lie."""
    calls: list[Any] = []
    real_open = pathlib.Path.open

    def spy(self: pathlib.Path, *args: Any, **kwargs: Any) -> Any:
        calls.append((self, args, kwargs))
        return real_open(self, *args, **kwargs)

    return spy, calls


class TestNoFilesystemWrite:
    """The explicit regression test for v1's D-ST-1 (`with open('stt.ogg',
    'wb')`, one fixed filename in the process CWD, raced by every worker in
    the 50-thread pool -- `Audio.py:23,25`).

    Asserting `not os.path.exists('stt.ogg')` would be decorative: v2 never
    even constructs that string, so its absence proves nothing about *why*
    -- a regression that wrote to a different filename, or via a different
    API, would sail through unnoticed. Instead this patches every stdlib
    entry point that opens a file for writing --

    * `builtins.open`, the literal call v1's defect used,
    * `pathlib.Path.open`, what most modern code (including third-party
      libraries) reaches for instead, and
    * `tempfile.NamedTemporaryFile` / `tempfile.mkstemp`, the indirect route
      a "just needs *a* file" call would take,

    -- with spies that still run the real implementation (so anything else
    legitimately happening in the interpreter during the test -- pytest's
    own machinery, structlog's stdout writer -- keeps working undisturbed),
    and asserts zero calls happened while the handler ran. A call count of
    zero is the only claim that actually rules out a hidden write anywhere
    in the path, not just under the literal filename v1 used.

    The collaborators one level below `transcribe.py` (`router().transcribe`,
    the OpenAI provider) are faked here, as they must be for a unit test --
    design.md's own claim that the provider takes `bytes` straight through
    to the SDK (`openai_provider.py:171-186`) is what makes D-ST-1
    structurally impossible below this seam, and is not this test's job to
    re-prove.
    """

    async def test_shape_a_voice_ai_never_opens_a_file(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _wire_happy_collaborators(monkeypatch)
        monkeypatch.setattr(transcribe, "llm_router", lambda: _fake_router())
        reply_with_ai = AsyncMock()
        monkeypatch.setattr(transcribe, "reply_with_ai", reply_with_ai)

        message = _FakeMessage(voice=_FakeVoice(), bot=_bot())

        open_spy = MagicMock(wraps=builtins.open)
        path_open_spy, path_open_calls = _path_open_spy()
        tempfile_spy = MagicMock(wraps=tempfile.NamedTemporaryFile)
        mkstemp_spy = MagicMock(wraps=tempfile.mkstemp)
        monkeypatch.setattr(builtins, "open", open_spy)
        monkeypatch.setattr(pathlib.Path, "open", path_open_spy)
        monkeypatch.setattr(tempfile, "NamedTemporaryFile", tempfile_spy)
        monkeypatch.setattr(tempfile, "mkstemp", mkstemp_spy)

        await transcribe.voice_ai(message, bot=message.bot)

        open_spy.assert_not_called()
        assert path_open_calls == []
        tempfile_spy.assert_not_called()
        mkstemp_spy.assert_not_called()
        reply_with_ai.assert_awaited_once()

    async def test_shape_b_transcribe_command_never_opens_a_file(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _ctx()
        _wire_happy_collaborators(monkeypatch, ctx=ctx)
        monkeypatch.setattr(transcribe, "llm_router", lambda: _fake_router())

        reply_target = _FakeMessage(voice=_FakeVoice())
        message = _FakeMessage(reply_to_message=reply_target, bot=_bot())

        open_spy = MagicMock(wraps=builtins.open)
        path_open_spy, path_open_calls = _path_open_spy()
        tempfile_spy = MagicMock(wraps=tempfile.NamedTemporaryFile)
        mkstemp_spy = MagicMock(wraps=tempfile.mkstemp)
        monkeypatch.setattr(builtins, "open", open_spy)
        monkeypatch.setattr(pathlib.Path, "open", path_open_spy)
        monkeypatch.setattr(tempfile, "NamedTemporaryFile", tempfile_spy)
        monkeypatch.setattr(tempfile, "mkstemp", mkstemp_spy)

        await transcribe.transcribe_command(message, parsed=_parsed(), skin="cookiebot")

        open_spy.assert_not_called()
        assert path_open_calls == []
        tempfile_spy.assert_not_called()
        mkstemp_spy.assert_not_called()
        reply_target.reply.assert_awaited_once()


# --------------------------------------------------- language hint (D-ST-5)


class TestLanguageReachesTranscribe:
    """D-ST-5: v1 passes no `language` hint at all (`Audio.py:26-30`) despite
    having the group's language in hand; v2 must pass `ctx.lang` through to
    `router().transcribe()` on both shapes."""

    async def test_voice_ai_passes_the_group_language(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _wire_happy_collaborators(monkeypatch, ctx=_ctx(lang="pt"))
        call = AsyncMock(return_value=_transcript())
        monkeypatch.setattr(transcribe, "llm_router", lambda: SimpleNamespace(transcribe=call))
        monkeypatch.setattr(transcribe, "reply_with_ai", AsyncMock())

        message = _FakeMessage(voice=_FakeVoice(), bot=_bot())
        await transcribe.voice_ai(message, bot=message.bot)

        call.assert_awaited_once()
        assert call.await_args is not None
        assert call.await_args.kwargs["language"] == "pt"
        assert call.await_args.kwargs["filename"] == "voice.ogg"
        assert call.await_args.kwargs["group_id"] == -100
        assert call.await_args.kwargs["tenant_id"] == "cookiebot"

    async def test_transcribe_command_passes_the_group_language(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _wire_happy_collaborators(monkeypatch, ctx=_ctx(lang="es"))
        call = AsyncMock(return_value=_transcript())
        monkeypatch.setattr(transcribe, "llm_router", lambda: SimpleNamespace(transcribe=call))

        reply_target = _FakeMessage(voice=_FakeVoice())
        message = _FakeMessage(reply_to_message=reply_target, bot=_bot())
        await transcribe.transcribe_command(message, parsed=_parsed(), skin="cookiebot")

        call.assert_awaited_once()
        assert call.await_args is not None
        assert call.await_args.kwargs["language"] == "es"


# ----------------------------------- shape (a): the transcript is never shown


class TestVoiceAiTranscriptNeverShown:
    """R1.6/D-ST-4: v1 assigns the transcript to `msg['text']` and hands it
    only to `conversational_ai` (`COOKIEBOT.py:161-162`) -- the user only
    ever sees the AI's reply, never the raw transcript, and there is no
    `.capitalize()` mangling it on the way."""

    async def test_transcript_reaches_reply_with_ai_and_message_reply_is_untouched(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _ctx()
        _wire_happy_collaborators(monkeypatch, ctx=ctx)
        transcript = _transcript(text="the raw transcript, lowercase and all")
        monkeypatch.setattr(transcribe, "llm_router", lambda: _fake_router(transcript))
        reply_with_ai = AsyncMock()
        monkeypatch.setattr(transcribe, "reply_with_ai", reply_with_ai)

        message = _FakeMessage(voice=_FakeVoice(), bot=_bot())
        await transcribe.voice_ai(
            message, bot=message.bot, skin="cookiebot", bot_username="CookieMWbot"
        )

        reply_with_ai.assert_awaited_once_with(
            message,
            ctx,
            skin="cookiebot",
            bot_username="CookieMWbot",
            text="the raw transcript, lowercase and all",
        )
        message.reply.assert_not_awaited()


# ----------------------------- shape (b): 4000-char truncation (R2.6)


class TestTranscribeCommandTruncation:
    """R2.6: Telegram caps a message at 4096 characters; a longer transcript
    is truncated to 4000 plus a single `…`, not split into a thread."""

    async def test_a_transcript_over_4000_chars_is_truncated_with_an_ellipsis(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _wire_happy_collaborators(monkeypatch)
        long_text = "x" * 5000
        monkeypatch.setattr(
            transcribe, "llm_router", lambda: _fake_router(_transcript(text=long_text))
        )

        reply_target = _FakeMessage(voice=_FakeVoice())
        message = _FakeMessage(reply_to_message=reply_target, bot=_bot())
        await transcribe.transcribe_command(message, parsed=_parsed(), skin="cookiebot")

        reply_target.reply.assert_awaited_once()
        (sent_text,), _kwargs = reply_target.reply.await_args
        assert sent_text == "x" * 4000 + "…"
        assert len(sent_text) == 4001

    async def test_a_transcript_at_exactly_4000_chars_is_not_truncated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _wire_happy_collaborators(monkeypatch)
        exact_text = "y" * 4000
        monkeypatch.setattr(
            transcribe, "llm_router", lambda: _fake_router(_transcript(text=exact_text))
        )

        reply_target = _FakeMessage(voice=_FakeVoice())
        message = _FakeMessage(reply_to_message=reply_target, bot=_bot())
        await transcribe.transcribe_command(message, parsed=_parsed(), skin="cookiebot")

        (sent_text,), _kwargs = reply_target.reply.await_args
        assert sent_text == exact_text
        assert "…" not in sent_text

    async def test_a_short_transcript_is_sent_verbatim(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _wire_happy_collaborators(monkeypatch)
        monkeypatch.setattr(
            transcribe, "llm_router", lambda: _fake_router(_transcript(text="short transcript"))
        )

        reply_target = _FakeMessage(voice=_FakeVoice())
        message = _FakeMessage(reply_to_message=reply_target, bot=_bot())
        await transcribe.transcribe_command(message, parsed=_parsed(), skin="cookiebot")

        reply_target.reply.assert_awaited_once_with("short transcript")

    async def test_the_reply_lands_on_the_voice_note_not_the_command(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """R2.5: `reply.reply(...)`, not `message.reply(...)` -- the
        transcript belongs next to the audio it transcribes."""
        _wire_happy_collaborators(monkeypatch)
        monkeypatch.setattr(transcribe, "llm_router", lambda: _fake_router())

        reply_target = _FakeMessage(voice=_FakeVoice())
        message = _FakeMessage(reply_to_message=reply_target, bot=_bot())
        await transcribe.transcribe_command(message, parsed=_parsed(), skin="cookiebot")

        reply_target.reply.assert_awaited_once()
        message.reply.assert_not_awaited()


# ------------------------------------------- transcribe_no_voice (R2.3)


class TestTranscribeNoVoice:
    """R2.3: the trigger used on anything that is not a reply to a voice
    note says so, rather than staying silent -- and never touches the
    download path to get there."""

    async def test_no_reply_target_at_all(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx = _ctx()
        monkeypatch.setattr(transcribe, "context_for", AsyncMock(return_value=ctx))
        download = AsyncMock()
        message = _FakeMessage(reply_to_message=None, bot=_bot(download=download))

        await transcribe.transcribe_command(message, parsed=_parsed(), skin="cookiebot")

        message.reply.assert_awaited_once_with(transcribe.t(ctx, "transcribe_no_voice"))
        download.assert_not_awaited()

    async def test_reply_target_exists_but_carries_no_voice(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _ctx()
        monkeypatch.setattr(transcribe, "context_for", AsyncMock(return_value=ctx))
        download = AsyncMock()
        reply_target = _FakeMessage(voice=None)
        message = _FakeMessage(reply_to_message=reply_target, bot=_bot(download=download))

        await transcribe.transcribe_command(message, parsed=_parsed(), skin="cookiebot")

        message.reply.assert_awaited_once_with(transcribe.t(ctx, "transcribe_no_voice"))
        download.assert_not_awaited()


# --------------------------------------------------- utility/fun gates (R2.2/R1.2)


class TestTranscribeCommandUtilityGate:
    """R2.2: `/transcribe` is gated on `utility`, not `fun`, and uses
    `deny_if_disabled`'s standard notice -- unlike shape (a)'s silence."""

    async def test_utility_off_replies_with_the_standard_notice_and_never_downloads(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _ctx(utility=False)
        monkeypatch.setattr(transcribe, "context_for", AsyncMock(return_value=ctx))
        download = AsyncMock()
        reply_target = _FakeMessage(voice=_FakeVoice())
        message = _FakeMessage(reply_to_message=reply_target, bot=_bot(download=download))

        await transcribe.transcribe_command(message, parsed=_parsed(), skin="cookiebot")

        message.reply.assert_awaited_once_with(transcribe.t(ctx, "utility_off"))
        download.assert_not_awaited()
        reply_target.reply.assert_not_awaited()


class TestVoiceAiFunGateIsSilentWhenClosed:
    """R1.2: unlike shape (b), a closed `fun` gate on the ported sub-step
    stays silent -- v1 sends no `fun_off` notice on this path either
    (`COOKIEBOT.py:160`, `do not use deny_if_disabled`). The gate lives on
    the *router registration* (`FeatureGate("fun")`, R1.1), not inside
    `voice_ai`'s body, so this pins both halves: that `voice_ai` really is
    registered behind that filter, and that the filter itself declines
    without ever replying."""

    def test_voice_ai_is_registered_behind_a_fun_feature_gate(self) -> None:
        handlers = transcribe.router.observers["message"].handlers
        voice_ai_handler = next(h for h in handlers if h.callback is transcribe.voice_ai)
        gates = [
            f.callback for f in voice_ai_handler.filters if isinstance(f.callback, FeatureGate)
        ]
        assert len(gates) == 1
        assert gates[0].area == "fun"

    async def test_the_feature_gate_declines_without_replying_when_fun_is_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import cb_gateway.context as gw_context

        monkeypatch.setattr(gw_context, "context_for", AsyncMock(return_value=_ctx(fun=False)))
        message = _FakeMessage(bot=_bot())

        allowed = await FeatureGate("fun")(message, bot=message.bot)

        assert allowed is False
        message.reply.assert_not_awaited()


# ------------------------------------------------ every error path replies (D-ST-6)


class TestErrorPathsAlwaysProduceAVisibleReply:
    """D-ST-6: v1's `speech_to_text` has no `try`/`except` at all -- any
    failure escapes to the dispatcher's own handler, which DMs the owner a
    traceback and leaves the chat silent (`COOKIEBOT.py:329-330`). Every
    failure path in `_get_transcript` -- a failed download, a raised
    `LLMError`, anything else the router might throw -- must instead reply
    `transcribe_failed`, the shared catch-all (design.md R5)."""

    async def test_download_returning_none_replies_with_transcribe_failed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(transcribe, "get_settings", lambda: _settings())
        ctx = _ctx()
        message = _FakeMessage()
        bot = _bot(download=AsyncMock(return_value=None))

        result = await transcribe._get_transcript(  # noqa: SLF001
            message,
            ctx,
            bot=bot,
            skin="cookiebot",
            voice=_FakeVoice(),
            shape="voice_ai",
            reply_target=message,
        )

        assert result is None
        message.reply.assert_awaited_once_with(transcribe.t(ctx, "transcribe_failed"))

    async def test_download_raising_also_replies_with_transcribe_failed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`bot.download` itself throwing (a dead Telegram file API, in
        `_download`'s own words) must funnel through the exact same catch --
        not escape as an unhandled exception."""
        monkeypatch.setattr(transcribe, "get_settings", lambda: _settings())
        ctx = _ctx()
        message = _FakeMessage()

        async def boom(file_id: str) -> None:
            raise RuntimeError("telegram file API is down")

        bot = _bot(download=boom)

        result = await transcribe._get_transcript(  # noqa: SLF001
            message,
            ctx,
            bot=bot,
            skin="cookiebot",
            voice=_FakeVoice(),
            shape="command",
            reply_target=message,
        )

        assert result is None
        message.reply.assert_awaited_once_with(transcribe.t(ctx, "transcribe_failed"))

    async def test_llm_error_replies_with_transcribe_failed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(transcribe, "get_settings", lambda: _settings())
        monkeypatch.setattr(
            transcribe.tenancy.registry, "by_skin", AsyncMock(return_value=_FakeTenant())
        )
        ctx = _ctx()
        monkeypatch.setattr(
            transcribe,
            "llm_router",
            lambda: _fake_router(side_effect=LLMError("provider down")),
        )
        message = _FakeMessage()

        result = await transcribe._get_transcript(  # noqa: SLF001
            message,
            ctx,
            bot=_bot(),
            skin="cookiebot",
            voice=_FakeVoice(),
            shape="voice_ai",
            reply_target=message,
        )

        assert result is None
        message.reply.assert_awaited_once_with(transcribe.t(ctx, "transcribe_failed"))

    async def test_an_unnarrow_exception_from_the_router_also_replies(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """D-ST-6's whole point: a bare `except Exception` here, not a narrow
        provider-exception allowlist -- a bug elsewhere (e.g. a timeout that
        is not an `LLMError` subclass) must still surface as a reply, not a
        swallowed crash."""
        monkeypatch.setattr(transcribe, "get_settings", lambda: _settings())
        monkeypatch.setattr(
            transcribe.tenancy.registry, "by_skin", AsyncMock(return_value=_FakeTenant())
        )
        ctx = _ctx()
        monkeypatch.setattr(
            transcribe,
            "llm_router",
            lambda: _fake_router(side_effect=TimeoutError("timed out")),
        )
        message = _FakeMessage()

        result = await transcribe._get_transcript(  # noqa: SLF001
            message,
            ctx,
            bot=_bot(),
            skin="cookiebot",
            voice=_FakeVoice(),
            shape="command",
            reply_target=message,
        )

        assert result is None
        message.reply.assert_awaited_once_with(transcribe.t(ctx, "transcribe_failed"))

    async def test_over_the_duration_cap_also_replies_visibly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The cap (R1.3/R2.4) is the fourth error path, already exercised in
        detail by `TestGetTranscriptDurationCap`; repeated here only to keep
        the "every error path replies" claim checkable as one group."""
        monkeypatch.setattr(transcribe, "get_settings", lambda: _settings(max_duration=10))
        ctx = _ctx()
        message = _FakeMessage()

        result = await transcribe._get_transcript(  # noqa: SLF001
            message,
            ctx,
            bot=_bot(),
            skin="cookiebot",
            voice=_FakeVoice(duration=11),
            shape="voice_ai",
            reply_target=message,
        )

        assert result is None
        message.reply.assert_awaited_once()

    async def test_transcribe_command_end_to_end_failure_still_replies(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One full-handler-level rendition of the same claim: a `/transcribe`
        whose download fails still leaves the chat with a visible reply, not
        silence."""
        _wire_happy_collaborators(monkeypatch)
        monkeypatch.setattr(
            transcribe, "llm_router", lambda: _fake_router(side_effect=LLMError("down"))
        )
        reply_target = _FakeMessage(voice=_FakeVoice())
        message = _FakeMessage(reply_to_message=reply_target, bot=_bot())

        await transcribe.transcribe_command(message, parsed=_parsed(), skin="cookiebot")

        message.reply.assert_awaited_once_with(transcribe.t(_ctx(), "transcribe_failed"))
        reply_target.reply.assert_not_awaited()

    async def test_voice_ai_end_to_end_failure_still_replies(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _wire_happy_collaborators(monkeypatch)
        monkeypatch.setattr(
            transcribe, "llm_router", lambda: _fake_router(side_effect=LLMError("down"))
        )
        reply_with_ai = AsyncMock()
        monkeypatch.setattr(transcribe, "reply_with_ai", reply_with_ai)

        message = _FakeMessage(voice=_FakeVoice(), bot=_bot())
        await transcribe.voice_ai(message, bot=message.bot)

        message.reply.assert_awaited_once_with(transcribe.t(_ctx(), "transcribe_failed"))
        reply_with_ai.assert_not_awaited()
