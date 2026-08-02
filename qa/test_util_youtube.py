"""Step definitions for util_youtube.

QA: qa/features/util_youtube.feature (synced from Cookiebot-QA/features/util_youtube.feature).
Contract: docs/contracts/util_youtube.md.

Drives the real dispatcher against the mock Telegram API. The search itself
is a cb-worker job (design R1.2); the arq broker `cb_gateway.queue.enqueue`
talks to is the outside world (AGENTS.md §6), so `fake_queue` monkeypatches
`cb_gateway.handlers.youtube`'s own reference to it — same seam
`qa/test_util_everyone.py`/`qa/test_util_calladms.py` already use for their
own worker halves.
"""

from __future__ import annotations

from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from pytest_bdd import given, parsers, scenarios, then, when

from cb_core import jobs
from cb_gateway.handlers import youtube as youtube_handler
from qa.conftest import GROUP_ID, USER_ID, Context, feed, make_message_update, next_update_id
from qa.mock_telegram import MockTelegram

scenarios("util_youtube.feature")


@pytest.fixture(autouse=True)
def fake_queue(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def _fake_enqueue(job: str, *args: object, **kwargs: object) -> bool:
        calls.append((job, kwargs))
        return True

    monkeypatch.setattr(youtube_handler, "enqueue", _fake_enqueue)
    return calls


@given("that the bot is in the group and properly set up")
def bot_set_up(ctx: Context) -> None:
    ctx.bot_running = True


@given("that the user is in the group")
def user_in_group() -> None:
    pass


@when(parsers.parse('the user sends the command "{text}"'))
def user_sends(
    ctx: Context, run: Any, dispatcher: Dispatcher, bot: Bot, telegram: MockTelegram, text: str
) -> None:
    feed(run, dispatcher, bot, make_message_update(text, next_update_id(), user_id=USER_ID))


@then("the bot should reply with a link to a youtube video about how to make a cake")
def enqueues_the_search(fake_queue: list[tuple[str, dict[str, Any]]]) -> None:
    assert len(fake_queue) == 1, fake_queue
    job, kwargs = fake_queue[0]
    assert job == jobs.YOUTUBE_SEARCH
    assert kwargs["group_id"] == GROUP_ID
    assert kwargs["query"] == "how to make a cake"
    assert kwargs["lang"] == "en"
