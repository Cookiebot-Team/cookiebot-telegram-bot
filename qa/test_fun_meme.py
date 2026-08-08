"""Step definitions for fun_meme.

QA: `qa/features/fun_meme.feature` — authored, not ported. Contract:
`docs/contracts/fun_meme.md`.

Drives the real dispatcher against the mock Telegram API; only the
gateway->worker queue is monkeypatched, the seam `qa/test_util_youtube.py`
established. The suite needs a real database because one scenario writes
`group_configs`.
"""

from __future__ import annotations

from types import ModuleType
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from pytest_bdd import given, parsers, scenarios, then, when

from cb_core import jobs
from cb_gateway.handlers import meme as meme_handler
from qa.conftest import (
    GROUP_ID,
    USER_ID,
    Context,
    feed,
    make_message_update,
    next_update_id,
)
from qa.mock_telegram import MockTelegram

scenarios("fun_meme.feature")


@pytest.fixture(autouse=True)
def fake_queue(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def _fake_enqueue(job: str, *args: object, **kwargs: object) -> bool:
        calls.append((job, dict(kwargs)))
        return True

    monkeypatch.setattr(meme_handler, "enqueue", _fake_enqueue)
    return calls


@pytest.fixture(autouse=True)
def _needs_db(database: ModuleType) -> None:
    """One scenario writes `group_configs`."""


def _sent(telegram: MockTelegram) -> list[dict[str, Any]]:
    return [
        call for call in telegram.calls_to("sendMessage") if int(call.get("chat_id", 0)) == GROUP_ID
    ]


@given("that the bot is in the group and properly set up")
def bot_set_up(ctx: Context) -> None:
    ctx.bot_running = True


@given("that the user is in the group")
def user_in_group() -> None:
    pass


@given("fun functions are disabled for the group")
def fun_off(run: Any) -> None:
    from cb_core import group_config

    run(group_config.set_config(GROUP_ID, functions_fun=False))


@when(parsers.parse('the user sends the command "{text}"'))
def user_sends(run: Any, dispatcher: Dispatcher, bot: Bot, text: str) -> None:
    feed(run, dispatcher, bot, make_message_update(text, next_update_id(), user_id=USER_ID))


def _only_job(queue: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    assert len(queue) == 1, queue
    job, kwargs = queue[0]
    assert job == jobs.COMPOSE_MEME
    assert kwargs["group_id"] == GROUP_ID
    assert kwargs["lang"] == "en"
    return kwargs


@then("the bot should hand the meme to the compositing job with no tags")
def hands_over_untagged(fake_queue: list[tuple[str, dict[str, Any]]]) -> None:
    assert _only_job(fake_queue)["tagged"] == []


@then("the bot should hand the meme to the compositing job tagging alice and bob")
def hands_over_tagged(fake_queue: list[tuple[str, dict[str, Any]]]) -> None:
    # v1's `get_members_tagged` splits on "@" and keeps the trailing text up to
    # the next "@", so the first tag carries a trailing space. Preserved by
    # `fun_battle.parse_tagged_targets`, which this handler imports.
    assert [t.strip() for t in _only_job(fake_queue)["tagged"]] == ["alice", "bob"]


@then("the bot should say more than five members is not possible")
def refuses_six(telegram: MockTelegram) -> None:
    from cb_core import locales

    assert _sent(telegram)[-1]["text"] == locales.get("meme_no", "en")


@then("the bot should say fun functions are off")
def fun_is_off(telegram: MockTelegram) -> None:
    from cb_core import locales

    assert _sent(telegram)[-1]["text"] == locales.get("fun_off", "en")


@then("should not hand anything to the compositing job")
def nothing_enqueued(fake_queue: list[tuple[str, dict[str, Any]]]) -> None:
    assert fake_queue == []
