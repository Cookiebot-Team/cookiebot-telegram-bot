"""Step definitions for util_birthday.

QA: qa/features/util_birthday.feature (synced from Cookiebot-QA/features/util_birthday.feature,
see that file's own header for the recorded bare-`/birthday` conflict).
Contract: docs/contracts/util_birthday.md.

Drives the real dispatcher against the mock Telegram API. The collage itself
is a cb-worker job (design R1); `fake_queue` monkeypatches
`cb_gateway.handlers.birthday`'s own reference to `enqueue`, same seam
`qa/test_util_everyone.py`/`qa/test_util_calladms.py`/`qa/test_util_youtube.py`
already use for their own worker halves.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Iterator
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from pytest_bdd import given, parsers, scenarios, then, when

from cb_core import birthdays, db, group_config, jobs, locales
from cb_gateway.handlers import birthday as birthday_handler
from qa.conftest import GROUP_ID, USER_ID, Context, feed, make_message_update, next_update_id

scenarios("util_birthday.feature")


@pytest.fixture(autouse=True)
def fake_queue(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def _fake_enqueue(job: str, *args: object, **kwargs: object) -> bool:
        calls.append((job, kwargs))
        return True

    monkeypatch.setattr(birthday_handler, "enqueue", _fake_enqueue)
    return calls


@pytest.fixture(autouse=True)
def _reset(run: Callable[[Coroutine[Any, Any, Any]], Any]) -> Iterator[None]:
    yield
    try:
        db.pool()
    except RuntimeError:
        return  # no database in this run; nothing was ever persisted either
    run(group_config.set_config(GROUP_ID, functions_fun=True))
    group_config._l1.clear()  # noqa: SLF001


class Ctx:
    def alloc_id(self) -> int:
        return next_update_id()


@pytest.fixture
def birthday_ctx() -> Ctx:
    return Ctx()


@given("that the bot is in the group and properly set up")
def bot_set_up(ctx: Context) -> None:
    ctx.bot_running = True


@given("that the user is in the group")
def user_in_group() -> None:
    pass


@given("the fun feature is turned off")
def fun_disabled(database: Any, run: Callable[[Coroutine[Any, Any, Any]], Any]) -> None:
    run(group_config.set_config(GROUP_ID, functions_fun=False))
    group_config._l1.clear()  # noqa: SLF001


@when(parsers.parse('the user sends the command "{text}"'))
def user_sends(
    birthday_ctx: Ctx,
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
    text: str,
) -> None:
    feed(run, dispatcher, bot, make_message_update(text, birthday_ctx.alloc_id(), user_id=USER_ID))


def _last_text(telegram: Any) -> str:
    sent = telegram.calls_to("sendMessage")
    assert sent, "expected a sendMessage call, got none"
    return str(sent[-1].get("text", ""))


@then("the bot should reply with a montage of users that has their birthday on that day")
def bare_command_asserts_v1_reality(
    telegram: Any, fake_queue: list[tuple[str, dict[str, Any]]]
) -> None:
    """The recorded conflict: v1's real behaviour for a bare `/birthday` is
    the `bday.title` prompt, never a montage — see the feature file's header."""
    assert _last_text(telegram) == birthdays.bday_title("en")
    assert not fake_queue, fake_queue


@then("the bot should enqueue the birthday collage job")
def enqueues_the_collage(fake_queue: list[tuple[str, dict[str, Any]]]) -> None:
    assert len(fake_queue) == 1, fake_queue
    job, kwargs = fake_queue[0]
    assert job == jobs.BIRTHDAY_COLLAGE
    assert kwargs["group_id"] == GROUP_ID
    assert kwargs["extra_names"] == ["@someone"]
    assert kwargs["lang"] == "en"


@then("the bot should reply with a message saying that the fun feature is turned off")
def bot_says_fun_off(telegram: Any, fake_queue: list[tuple[str, dict[str, Any]]]) -> None:
    assert _last_text(telegram) == locales.get("fun_off", "en")
    assert not fake_queue, fake_queue
