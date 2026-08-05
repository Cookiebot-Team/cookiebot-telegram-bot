"""Step definitions for x_speech_to_text.

QA: qa/features/x_speech_to_text.feature -- authored here, not ported.
`.specs/features/x_speech_to_text/spec.md`'s "Shape (b)" section is the
source of intent for the net-new `/transcribe` command (no v1 or
Cookiebot-QA scenario exists for either shape at all -- the only
voice-adjacent QA file is `core_musicdetection.feature`, Shazam, a different
function in the same v1 file, out of scope here); `design.md`'s R1 (the
ported voice-to-AI sub-step) and R2 (the standalone command) are the source
of the exact mechanics these scenarios pin, per tasks.md T5.

Drives the real dispatcher (`cb_gateway.main.dp`, via `qa/conftest.py`'s
session-scoped `dispatcher` fixture) against the mock Telegram API, same as
every other acceptance test in this suite (AGENTS.md SS6: "no mocking of our
own code in acceptance tests. Mock the outside world only"). Two outside
worlds get mocked here:

- The LLM, twice over: `cb_gateway.handlers.transcribe` resolves its own
  router through a module-level `llm_router` name for `.transcribe(...)`,
  and shape (a) additionally funnels the transcript into
  `chat_ai.reply_with_ai`, which resolves *its* router through `chat_ai`'s
  own module-level `llm_router` name for `.complete(...)`. Both get the same
  `monkeypatch.setattr(<module>, "llm_router", lambda: <fake>)` treatment
  `qa/test_x_conversational_ai.py` and `packages/cb-gateway/tests/
  test_chat_ai.py` already established, applied to both seams rather than
  inventing a second pattern.
- `Bot.download`, the one network call `qa/mock_telegram.py` does not
  implement (it has no `getFile`, per `qa/test_fun_random.py`'s own
  docstring) -- faked the same way `qa/integration/test_fun_random.py`'s
  `_FakeBot.download` and `packages/cb-gateway/tests/test_transcribe.py`'s
  `_bot(download=...)` fake it, just applied to the real session-scoped
  `Bot` via `monkeypatch.setattr(bot, "download", ...)` instead of a
  hand-rolled stand-in class, since this suite drives the real dispatcher and
  the real `Bot` instance is exactly what `transcribe.py`'s `_download`
  calls `.download()` on.

A real Postgres, like `x_conversational_ai`, *is* needed by every scenario
that sends a voice note -- but for a different filter than that suite's own
reason. `mediarestrict.enforce_media_restriction` is registered on
`_RESTRICTED_CONTENT`, which includes `F.voice`, and sits ahead of
`transcribe.router` in `build_router`'s join-chain section. With
`media_restrict_seconds` at its v1-matching default (600, on by default for
the QA group), it runs `_joined_at`'s `db.fetchrow` over *every* voice note,
none of which is about media restriction. With no live pool that call
raises `RuntimeError` instead of returning "no `group_members` row",
crashing the scenario outright rather than the fail-open `SkipHandler`
`enforce_media_restriction` intends for an unknown join time. The
`_database` fixture below requests the real `database` fixture purely to
give that filter something to query "no row" against, and skips this whole
file cleanly (AGENTS.md SS6) when no database is reachable.
`tenancy.registry.by_skin` needs no such fixture: it fails open to
`FALLBACK` with no pool at all (`cb_core/tenancy.py`'s own `except
Exception` clause), and falls back the same way with a pool but no seeded
`tenants` row.

Every update in this file takes its id from `next_update_id()` (via `Ctx.
alloc_id()`) -- the dedupe middleware is real and a reused id is dropped as a
redelivery, which reads exactly like "the bot said nothing" (tasks.md T7's
own warning, equally true here).
"""

from __future__ import annotations

import dataclasses
import io
import json
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from cb_core import group_config
from cb_core.llm.types import Completion, Transcript, Usage
from cb_gateway.handlers import chat_ai, transcribe
from qa.conftest import BOT_USERNAME, GROUP_ID, USER_ID, feed, make_message_update, next_update_id

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from aiogram import Bot, Dispatcher

    from qa.conftest import Context
    from qa.mock_telegram import MockTelegram

scenarios("x_speech_to_text.feature")

# qa/conftest.py's TEST_TOKEN is "424242:TEST"; aiogram's Bot.id is parsed
# from the token's leading digits, same id `test_x_conversational_ai.py`'s
# own `_BOT_FROM` uses.
_BOT_FROM = {
    "id": 424242,
    "is_bot": True,
    "first_name": "Cookiebot",
    "username": BOT_USERNAME,
}

_STUB_AUDIO_BYTES = b"stub-ogg-bytes"


def _completion(text: str) -> Completion:
    return Completion(text=text, model="stub-model", provider="stub", usage=Usage())


def _transcript(text: str) -> Transcript:
    return Transcript(text=text, model="whisper-1", provider="openai", language="en")


class Ctx:
    """Per-scenario state on top of qa/conftest.py's base `Context`."""

    def __init__(self) -> None:
        # `SimpleNamespace`, not a hand-rolled class -- `test_transcribe.py`'s
        # own `_fake_router` uses the same shape, and a mutable attribute is
        # all the `Given` steps below need to swap the return value mid-scenario.
        self.fake_transcribe_router = SimpleNamespace(
            transcribe=AsyncMock(return_value=_transcript("stub transcript, unused by default"))
        )
        self.fake_chat_router = SimpleNamespace(complete=AsyncMock(return_value=_completion("ok")))
        self.download = AsyncMock(return_value=io.BytesIO(_STUB_AUDIO_BYTES))
        self.transcript_text: str | None = None
        self.voice_message_id: int | None = None

    def alloc_id(self) -> int:
        # Shared process-wide counter: a per-scenario counter collides with
        # earlier scenarios and the dedupe middleware drops the update.
        return next_update_id()


@pytest.fixture
def st_ctx() -> Ctx:
    return Ctx()


@pytest.fixture(autouse=True)
def _database(database: object) -> None:
    """See the module docstring -- this feature owns no table of its own;
    the real database is here only so `mediarestrict`'s restriction filter
    has a live pool to query "no group_members row" against."""


@pytest.fixture(autouse=True)
def _stub_llm_routers(st_ctx: Ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    """The two seams described in the module docstring: `transcribe.py`'s
    own `.transcribe(...)` call, and the `.complete(...)` call
    `chat_ai.reply_with_ai` makes once shape (a) hands it the transcript.
    Neither may ever reach a real provider (the task's own instruction)."""
    monkeypatch.setattr(transcribe, "llm_router", lambda: st_ctx.fake_transcribe_router)
    monkeypatch.setattr(chat_ai, "llm_router", lambda: st_ctx.fake_chat_router)


@pytest.fixture(autouse=True)
def _stub_download(st_ctx: Ctx, bot: Bot, monkeypatch: pytest.MonkeyPatch) -> None:
    """`Bot.download` is the outside world (Telegram's file API), not our own
    code -- see the module docstring for why it is faked directly on the
    real session-scoped `bot` rather than routed through `MockTelegram`.
    `monkeypatch` reverts this on the session-scoped `bot` after every single
    test function, so no other suite in the same session ever sees it."""
    monkeypatch.setattr(bot, "download", st_ctx.download)


def _bot_message(message_id: int) -> dict[str, Any]:
    """A message the bot itself sent, fabricated directly like
    `test_x_conversational_ai.py`'s own `bot_already_sent_plain_message` --
    `ReplyToBotFilter` only ever reads `reply_to_message.from_user.id`, so
    there is nothing to gain from routing this through a live send."""
    return {
        "message_id": message_id,
        "date": int(time.time()),
        "chat": {"id": GROUP_ID, "type": "supergroup", "title": "QA Group"},
        "from": _BOT_FROM,
        "text": "a message from the bot",
    }


def _voice_reply_target(message_id: int, *, duration: int = 10) -> dict[str, Any]:
    """A voice note some group member sent, fabricated the same way -- the
    reply chain `/transcribe` walks only ever reads `.voice` and hands the
    dict straight to `reply.reply(...)`, never the sender's identity."""
    return {
        "message_id": message_id,
        "date": int(time.time()),
        "chat": {"id": GROUP_ID, "type": "supergroup", "title": "QA Group"},
        "from": {"id": USER_ID, "is_bot": False, "first_name": "Tester", "username": "tester"},
        "voice": {
            "file_id": "voice-reply-target",
            "file_unique_id": "uvo-reply",
            "duration": duration,
            "mime_type": "audio/ogg",
        },
    }


# --------------------------------------------------------------------- given


@given("that the bot is in the group and properly set up")
def bot_set_up(ctx: Context) -> None:
    ctx.bot_running = True


@given(parsers.parse('the transcript will be "{text}"'))
def transcript_will_be(st_ctx: Ctx, text: str) -> None:
    st_ctx.transcript_text = text
    st_ctx.fake_transcribe_router.transcribe = AsyncMock(return_value=_transcript(text))


@given(parsers.parse('the AI will answer with "{text}"'))
def ai_will_answer(st_ctx: Ctx, text: str) -> None:
    st_ctx.fake_chat_router.complete = AsyncMock(return_value=_completion(text))


@given("the fun feature is turned off in the group")
def fun_off() -> None:
    """Same seam `qa/test_x_conversational_ai.py`'s own `fun_off` step uses
    (`group_config._l1` directly, no database needed): shape (a) is gated on
    `FeatureGate("fun")` exactly like `chat_ai.ai_reply` is, and this proves
    the voice path's silence the same structural way -- via a real dispatch,
    not by inspecting the router's filter list or calling `FeatureGate`
    directly (the gap Finding 4 closes; `packages/cb-gateway/tests/
    test_transcribe.py::TestVoiceAiFunGateIsSilentWhenClosed` still covers
    those narrower checks at the unit layer)."""
    config = dataclasses.replace(group_config.DEFAULTS, group_id=GROUP_ID, functions_fun=False)
    group_config._l1[GROUP_ID] = (config, time.monotonic() + 9999)  # noqa: SLF001


# ---------------------------------------------------------------------- when


@when("the user sends a voice note replying to a message from the bot")
def user_sends_voice_reply_to_bot(
    st_ctx: Ctx, run: Callable[[Coroutine[Any, Any, Any]], Any], dispatcher: Dispatcher, bot: Bot
) -> None:
    bot_msg = _bot_message(st_ctx.alloc_id())
    feed(
        run,
        dispatcher,
        bot,
        make_message_update(None, st_ctx.alloc_id(), reply_to=bot_msg, voice=10),
    )


@when("the user sends a voice note that is not a reply to anything")
def user_sends_voice_no_reply(
    st_ctx: Ctx, run: Callable[[Coroutine[Any, Any, Any]], Any], dispatcher: Dispatcher, bot: Bot
) -> None:
    feed(run, dispatcher, bot, make_message_update(None, st_ctx.alloc_id(), voice=10))


@when(parsers.parse('the user replies to a voice note with "{command}"'))
def user_replies_to_voice_note(
    st_ctx: Ctx,
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
    command: str,
) -> None:
    voice_msg = _voice_reply_target(st_ctx.alloc_id())
    st_ctx.voice_message_id = voice_msg["message_id"]
    feed(run, dispatcher, bot, make_message_update(command, st_ctx.alloc_id(), reply_to=voice_msg))


@when(parsers.parse('the user sends "{command}" without replying to anything'))
def user_sends_command_no_reply(
    st_ctx: Ctx,
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
    command: str,
) -> None:
    feed(run, dispatcher, bot, make_message_update(command, st_ctx.alloc_id()))


@when(
    "the user sends a voice note over the transcription limit, replying to a message from the bot"
)
def user_sends_over_length_voice_reply_to_bot(
    st_ctx: Ctx, run: Callable[[Coroutine[Any, Any, Any]], Any], dispatcher: Dispatcher, bot: Bot
) -> None:
    # Settings default (`transcribe_max_duration_seconds`) is 300 -- one
    # second over is enough to prove the cap, and keeps the scenario honest
    # about the real default rather than a value only the test knows.
    from cb_core.settings import get_settings

    over_the_cap = get_settings().transcribe_max_duration_seconds + 1
    bot_msg = _bot_message(st_ctx.alloc_id())
    feed(
        run,
        dispatcher,
        bot,
        make_message_update(None, st_ctx.alloc_id(), reply_to=bot_msg, voice=over_the_cap),
    )


# ---------------------------------------------------------------------- then


@then(parsers.parse('the bot replies with "{text}"'))
def bot_replies_with(telegram: MockTelegram, text: str) -> None:
    sent = telegram.calls_to("sendMessage")
    assert sent, "expected a sendMessage call, got none"
    assert str(sent[-1].get("text", "")) == text, sent[-1]


@then("the bot sends nothing at all")
def bot_sends_nothing(telegram: MockTelegram) -> None:
    assert not telegram.calls_to("sendMessage"), telegram.calls
    assert not telegram.calls_to("sendChatAction"), telegram.calls


@then("the transcript itself is never sent to the chat")
def transcript_never_sent(telegram: MockTelegram, st_ctx: Ctx) -> None:
    sent = telegram.calls_to("sendMessage")
    # R1.6/D-ST-4: the transcript is fed to `reply_with_ai` and never shown
    # itself -- exactly one message goes out (the AI's reply), and its text
    # is never the raw transcript.
    assert len(sent) == 1, sent
    assert sent[0].get("text", "") != st_ctx.transcript_text, sent[0]


@then(parsers.parse('the bot replies to the voice note with "{text}"'))
def bot_replies_to_voice_note(telegram: MockTelegram, st_ctx: Ctx, text: str) -> None:
    sent = telegram.calls_to("sendMessage")
    assert sent, "expected a sendMessage call, got none"
    last = sent[-1]
    assert str(last.get("text", "")) == text, last
    # R2.5: the reply lands on the voice note, not the command. aiogram's
    # `Message.reply()` addresses its target through the Bot API 7.0
    # `reply_parameters` field (a JSON-object form value, same as
    # `sendMediaGroup`'s `media` field elsewhere in this suite), not the
    # deprecated `reply_to_message_id`.
    raw_reply_parameters = last.get("reply_parameters", "{}")
    reply_parameters = (
        json.loads(raw_reply_parameters)
        if isinstance(raw_reply_parameters, str)
        else (raw_reply_parameters or {})
    )
    assert int(reply_parameters.get("message_id", -1)) == st_ctx.voice_message_id, last


@then("the transcript is never generated")
def transcript_never_generated(st_ctx: Ctx) -> None:
    # D-ST-3: the duration cap is checked before anything is downloaded or
    # transcribed -- an over-length note costs neither.
    st_ctx.download.assert_not_awaited()
    st_ctx.fake_transcribe_router.transcribe.assert_not_awaited()


@then("the model is never asked")
def model_never_asked(st_ctx: Ctx) -> None:
    # Finding 4: a closed `fun` gate must stop `voice_ai` before
    # `reply_with_ai` ever calls `chat_ai`'s router, same as it already stops
    # `transcribe` from being called at all.
    st_ctx.fake_chat_router.complete.assert_not_awaited()
