"""Step definitions for x_distortion.

QA: `qa/features/x_distortion.feature` — authored, not ported. Contract:
`docs/contracts/x_distortion.md`.

Drives the real dispatcher against the mock Telegram API. The one thing
monkeypatched is the gateway->worker queue, exactly as
`qa/test_util_youtube.py`/`test_util_calladms.py` do for their own worker
halves: the broker is the outside world (AGENTS.md §6), and what this layer
has to prove is that the right file, kind and language crossed it.

The suite asks for a real database because one scenario flips
`functions_fun` off, which is a `group_configs` write.
"""

from __future__ import annotations

from types import ModuleType
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from pytest_bdd import given, parsers, scenarios, then, when

from cb_core import jobs
from cb_gateway.handlers import destroy as destroy_handler
from qa.conftest import (
    GROUP_ID,
    USER_ID,
    Context,
    feed,
    make_message_update,
    next_update_id,
)
from qa.mock_telegram import MockTelegram

scenarios("x_distortion.feature")

PROFILE_FILE_ID = "profile-photo-1"


@pytest.fixture(autouse=True)
def fake_queue(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def _fake_enqueue(job: str, *args: object, **kwargs: object) -> bool:
        calls.append((job, dict(kwargs)))
        return True

    monkeypatch.setattr(destroy_handler, "enqueue", _fake_enqueue)
    return calls


@pytest.fixture(autouse=True)
def _needs_db(database: ModuleType) -> None:
    """One scenario writes `group_configs`; the rest read it."""


def _sent(telegram: MockTelegram) -> list[dict[str, Any]]:
    return [
        call for call in telegram.calls_to("sendMessage") if int(call.get("chat_id", 0)) == GROUP_ID
    ]


def _reply_stub(**payload: Any) -> dict[str, Any]:
    """A `reply_to_message` carrying one media field.

    Hand-built rather than taken from `qa.conftest.make_message_update`: that
    helper builds a whole *update*, and what a reply needs is the inner
    message object with exactly one media key on it.
    """
    base: dict[str, Any] = {
        "message_id": next_update_id(),
        "date": 1_700_000_000,
        "chat": {"id": GROUP_ID, "type": "supergroup", "title": "QA Group"},
        "from": {"id": USER_ID, "is_bot": False, "first_name": "Tester", "username": "tester"},
    }
    base.update(payload)
    return base


MEDIA: dict[str, dict[str, Any]] = {
    "a photo": {
        "photo": [
            {"file_id": "photo-small", "file_unique_id": "ps", "width": 90, "height": 90},
            {"file_id": "photo-large", "file_unique_id": "pl", "width": 800, "height": 600},
        ]
    },
    "a voice note": {"voice": {"file_id": "voice-1", "file_unique_id": "uv", "duration": 3}},
    "a sticker": {
        "sticker": {
            "file_id": "sticker-1",
            "file_unique_id": "us",
            "width": 512,
            "height": 512,
            "is_animated": False,
            "is_video": False,
            "type": "regular",
        }
    },
    "a video": {
        "video": {
            "file_id": "video-1",
            "file_unique_id": "uvi",
            "width": 320,
            "height": 240,
            "duration": 3,
        }
    },
    "an animation": {
        "animation": {
            "file_id": "gif-1",
            "file_unique_id": "ug",
            "width": 320,
            "height": 240,
            "duration": 3,
        }
    },
}


# ---------------------------------------------------------------------- given


@given("that the bot is in the group and properly set up")
def bot_set_up(ctx: Context) -> None:
    ctx.bot_running = True


@given("that the user is in the group")
def user_in_group() -> None:
    pass


@given("the user has a profile picture")
def has_profile_picture(telegram: MockTelegram) -> None:
    telegram.set_profile_photo(USER_ID, PROFILE_FILE_ID)


@given("fun functions are disabled for the group")
def fun_off(run: Any) -> None:
    from cb_core import group_config

    run(group_config.set_config(GROUP_ID, functions_fun=False))


# ----------------------------------------------------------------------- when


@when(parsers.parse('the user sends the command "{text}"'))
def user_sends(run: Any, dispatcher: Dispatcher, bot: Bot, text: str) -> None:
    feed(run, dispatcher, bot, make_message_update(text, next_update_id(), user_id=USER_ID))


@when(parsers.parse('the user replies to {what} with "{text}"'))
def user_replies_to_media(run: Any, dispatcher: Dispatcher, bot: Bot, what: str, text: str) -> None:
    feed(
        run,
        dispatcher,
        bot,
        make_message_update(
            text, next_update_id(), user_id=USER_ID, reply_to=_reply_stub(**MEDIA[what])
        ),
    )


# ----------------------------------------------------------------------- then


@then("the bot should explain what to reply to")
def explains_usage(telegram: MockTelegram) -> None:
    assert "Reply to a photo, audio or sticker with the command" in _sent(telegram)[-1]["text"]


def _only_job(queue: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    assert len(queue) == 1, queue
    job, kwargs = queue[0]
    assert job == jobs.DISTORT_MEDIA
    assert kwargs["group_id"] == GROUP_ID
    assert kwargs["lang"] == "en"
    return kwargs


@then("the bot should hand the photo to the distortion job")
def hands_over_the_photo(fake_queue: list[tuple[str, dict[str, Any]]]) -> None:
    kwargs = _only_job(fake_queue)
    # v1 resolves `'photo'` to the largest size, never the thumbnail.
    assert (kwargs["kind"], kwargs["file_id"]) == ("photo", "photo-large")


@then("the bot should hand the audio to the distortion job")
def hands_over_the_audio(fake_queue: list[tuple[str, dict[str, Any]]]) -> None:
    kwargs = _only_job(fake_queue)
    assert (kwargs["kind"], kwargs["file_id"]) == ("audio", "voice-1")


@then("the bot should hand the sticker to the distortion job")
def hands_over_the_sticker(fake_queue: list[tuple[str, dict[str, Any]]]) -> None:
    kwargs = _only_job(fake_queue)
    assert (kwargs["kind"], kwargs["file_id"]) == ("sticker", "sticker-1")


@then("the bot should hand the profile picture to the distortion job")
def hands_over_the_profile_picture(fake_queue: list[tuple[str, dict[str, Any]]]) -> None:
    kwargs = _only_job(fake_queue)
    assert (kwargs["kind"], kwargs["file_id"]) == ("photo", PROFILE_FILE_ID)


@then("the bot should say video distortion is disabled")
def video_disabled(telegram: MockTelegram) -> None:
    assert _sent(telegram)[-1]["text"] == "Video distortioning is currently disabled."


@then("the bot should say GIF distortion is disabled")
def gif_disabled(telegram: MockTelegram) -> None:
    assert _sent(telegram)[-1]["text"] == "GIF distortioning is currently disabled."


@then("the bot should say a profile picture is needed")
def needs_a_profile_picture(telegram: MockTelegram) -> None:
    from cb_core import locales

    assert _sent(telegram)[-1]["text"] == locales.get("battle_no_picture", "en")


@then("should not hand anything to the distortion job")
def nothing_enqueued(fake_queue: list[tuple[str, dict[str, Any]]]) -> None:
    assert fake_queue == []


@then("the bot should say fun functions are off")
def fun_is_off(telegram: MockTelegram) -> None:
    from cb_core import locales

    assert _sent(telegram)[-1]["text"] == locales.get("fun_off", "en")
