"""Step definitions for core_musicdetection.

QA: `qa/features/core_musicdetection.feature` — authored, not ported.
Contract: `docs/contracts/core_musicdetection.md`.

Drives the real dispatcher against the mock Telegram API. Only the
gateway->worker queue is monkeypatched (AGENTS.md §6), the same seam
`qa/test_util_youtube.py` uses.

The "still reaches the handlers below" scenario is the one worth explaining.
v1 runs the music check and the transcribe→AI sub-step from the *same* `voice`
branch (`COOKIEBOT.py:156-162`), so this handler must yield rather than
consume. What proves it here is that a voice note replying to the bot still
reaches `transcribe.voice_ai` downstream — which, with no LLM configured in
this harness, answers `transcribe_failed`. That reply is only reachable if the
update got past `musicdetection.router`.

Needs a real database: `core_mediarestrict`'s join-time lookup is registered
ahead of these routers and raises rather than failing open with no pool (the
same reason `qa/test_x_speech_to_text.py` gives), and one scenario writes
`group_configs`.
"""

from __future__ import annotations

from types import ModuleType
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from pytest_bdd import given, scenarios, then, when

from cb_core import jobs
from cb_core.settings import get_settings
from cb_gateway.handlers import musicdetection as music_handler
from qa.conftest import (
    BOT_USERNAME,
    GROUP_ID,
    USER_ID,
    Context,
    feed,
    make_message_update,
    next_update_id,
)
from qa.mock_telegram import MockTelegram

scenarios("core_musicdetection.feature")

BOT_ID = 424242


@pytest.fixture(autouse=True)
def fake_queue(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def _fake_enqueue(job: str, *args: object, **kwargs: object) -> bool:
        calls.append((job, dict(kwargs)))
        return True

    monkeypatch.setattr(music_handler, "enqueue", _fake_enqueue)
    return calls


@pytest.fixture(autouse=True)
def _needs_db(database: ModuleType) -> None:
    """See the module docstring."""


def _with_flag(monkeypatch: pytest.MonkeyPatch, *, enabled: bool) -> None:
    """`get_settings` is `lru_cache`d, so the flag is swapped at the handler's
    own reference rather than in the environment — the seam every other suite
    that needs a non-default setting uses."""
    base = get_settings()
    patched = base.model_copy(update={"music_detection_enabled": enabled})
    monkeypatch.setattr(music_handler, "get_settings", lambda: patched)


def _sent(telegram: MockTelegram) -> list[dict[str, Any]]:
    return [
        call for call in telegram.calls_to("sendMessage") if int(call.get("chat_id", 0)) == GROUP_ID
    ]


# ---------------------------------------------------------------------- given


@given("that the bot is in the group and properly set up")
def bot_set_up(ctx: Context) -> None:
    ctx.bot_running = True


@given("that music detection is switched on")
def detection_on(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_flag(monkeypatch, enabled=True)


@given("that music detection is switched off")
def detection_off(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_flag(monkeypatch, enabled=False)


@given("utility functions are disabled for the group")
def utility_off(run: Any) -> None:
    from cb_core import group_config

    run(group_config.set_config(GROUP_ID, functions_utility=False))


# ----------------------------------------------------------------------- when


@when("the user sends a voice note")
def sends_voice(run: Any, dispatcher: Dispatcher, bot: Bot) -> None:
    feed(run, dispatcher, bot, make_message_update(None, next_update_id(), voice=3))


@when('the user sends the message "just talking"')
def sends_text(run: Any, dispatcher: Dispatcher, bot: Bot) -> None:
    feed(run, dispatcher, bot, make_message_update("just talking", next_update_id()))


# ----------------------------------------------------------------------- then


@then("the bot should hand the voice note to the recognition job")
def hands_over(fake_queue: list[tuple[str, dict[str, Any]]]) -> None:
    assert len(fake_queue) == 1, fake_queue
    job, kwargs = fake_queue[0]
    assert job == jobs.IDENTIFY_MUSIC
    assert kwargs["group_id"] == GROUP_ID
    assert kwargs["file_id"] == "voice-1"
    assert kwargs["lang"] == "en"


@then("the bot should not hand anything to the recognition job")
def nothing_enqueued(fake_queue: list[tuple[str, dict[str, Any]]]) -> None:
    assert fake_queue == []


@then("the bot should say nothing at all")
def says_nothing(telegram: MockTelegram) -> None:
    """v1 dispatches this from a bare `if utilityfunctions:` with no `else`
    (`COOKIEBOT.py:156`) — a voice note never asked for anything."""
    assert _sent(telegram) == []


@then("the update should still reach the handlers registered after it")
def reaches_the_handlers_below(
    run: Any,
    dispatcher: Dispatcher,
    bot: Bot,
    telegram: MockTelegram,
    fake_queue: list[tuple[str, dict[str, Any]]],
) -> None:
    reply_to = {
        "message_id": next_update_id(),
        "date": 1_700_000_000,
        "chat": {"id": GROUP_ID, "type": "supergroup", "title": "QA Group"},
        "from": {
            "id": BOT_ID,
            "is_bot": True,
            "first_name": "Cookiebot",
            "username": BOT_USERNAME,
        },
        "text": "something the bot said",
    }
    feed(
        run,
        dispatcher,
        bot,
        make_message_update(None, next_update_id(), user_id=USER_ID, voice=3, reply_to=reply_to),
    )

    # It was fingerprinted...
    assert len(fake_queue) == 2, fake_queue
    # ...and `transcribe.voice_ai` downstream still saw it. With no LLM
    # configured in this harness the transcription fails, which is exactly the
    # branch that answers `transcribe_failed` — a reply only reachable if this
    # handler yielded.
    from cb_core import locales

    assert _sent(telegram), "nothing downstream saw the voice note"
    assert _sent(telegram)[-1]["text"] == locales.get("transcribe_failed", "en")
