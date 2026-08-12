"""Step definitions for x_fortune_cookie.

QA: qa/features/x_fortune_cookie.feature (authored here — Cookiebot-QA has no
scenario for this feature; see the file's own header).

The delete-then-answer tail runs on a background `asyncio.Task`
(fortune.py's module docstring, deviation 1) — driven to completion here the
same way `qa/test_fun_complaint.py` drives its own hold tail: monkeypatch the
module's `_sleep` seam to an instant no-op, snapshot `_pending_tails` before
feeding the update, then `asyncio.gather` the diff.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from cb_core import group_config, locales
from cb_gateway.handlers import fortune
from qa.conftest import GROUP_ID, feed, make_message_update, next_update_id

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Iterator

    from aiogram import Bot, Dispatcher

    from qa.conftest import Context
    from qa.mock_telegram import MockTelegram

scenarios("x_fortune_cookie.feature")


@pytest.fixture(autouse=True)
def _zero_delete_delay(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Same idiom as `qa/test_fun_complaint.py`'s `_zero_hold_delay`: the
    tail's `asyncio.sleep` is a module attribute so a test can replace it
    instead of waiting out the real 3 seconds."""

    async def _instant_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(fortune, "_sleep", _instant_sleep)
    yield


class Ctx:
    """Per-scenario state on top of qa/conftest.py's base `Context`."""

    def __init__(self) -> None:
        self.pending_before: set[asyncio.Task[None]] = set()

    def alloc_id(self) -> int:
        return next_update_id()


@pytest.fixture
def fortune_ctx() -> Ctx:
    return Ctx()


@given("that the bot is in the group and properly set up")
def bot_set_up(ctx: Context) -> None:
    ctx.bot_running = True


@given("that a user is in the group")
def user_in_group() -> None:
    pass


@given("that fun functions are disabled for the group")
def fun_disabled(database: Any, run: Callable[[Coroutine[Any, Any, Any]], Any]) -> None:
    run(group_config.set_config(GROUP_ID, functions_fun=False))


@when(parsers.parse('a user sends the command "{text}"'))
def user_sends(
    fortune_ctx: Ctx,
    ctx: Context,
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
    text: str,
) -> None:
    # Snapshot before feeding, same reasoning as fun_complaint's own step: the
    # tail's task is added to `_pending_tails` synchronously inside the
    # handler, before it returns.
    fortune_ctx.pending_before = set(fortune._pending_tails)  # noqa: SLF001
    feed(run, dispatcher, bot, make_message_update(text, ctx.update_id))


@then("the bot sends the fortune animation")
def bot_sends_animation(telegram: MockTelegram) -> None:
    sent = telegram.calls_to("sendAnimation")
    assert sent, "expected a sendAnimation call, got none"
    assert sent[-1].get("animation", "") == fortune._ANIMATION_URL  # noqa: SLF001


@then("the bot eventually deletes the animation and replies with a fortune")
def bot_eventually_replies(
    fortune_ctx: Ctx,
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    telegram: MockTelegram,
) -> None:
    # Drive the scheduled tail to completion — `_zero_delete_delay` only makes
    # the sleep instant, the task still needs the loop's cooperation to run.
    tasks = fortune._pending_tails - fortune_ctx.pending_before  # noqa: SLF001
    if tasks:

        async def _await_tail_tasks() -> None:
            await asyncio.gather(*tasks)

        run(_await_tail_tasks())

    deleted = telegram.calls_to("deleteMessage")
    assert deleted, "expected the animation to be deleted, got no deleteMessage call"

    sent = telegram.calls_to("sendMessage")
    assert sent, "expected a sendMessage call, got none"
    text = str(sent[-1].get("text", ""))
    lines = set(locales.lines("sorte", "en"))
    assert any(line in text for line in lines), text
    # v1: `send_message(..., parse_mode='HTML')` (Miscellaneous.py:375).
    assert sent[-1].get("parse_mode") == "HTML", sent[-1]


@then("the bot replies that fun functions are off")
def bot_says_fun_off(telegram: MockTelegram) -> None:
    sent = telegram.calls_to("sendMessage")
    assert sent, "expected a sendMessage call, got none"
    assert sent[-1].get("text", "") == locales.get("fun_off", "en")


@when("the bot draws lucky numbers many times")
def draw_many(ctx: Context) -> None:
    ctx.draws = [fortune.pick_lucky_numbers() for _ in range(500)]  # type: ignore[attr-defined]


@then("every draw has six numbers between 1 and 99 from six different tens-decades")
def check_draws(ctx: Context) -> None:
    draws: list[list[int]] = ctx.draws  # type: ignore[attr-defined]
    for numbers in draws:
        assert len(numbers) == 6, numbers
        assert all(1 <= n <= 99 for n in numbers), numbers
        decades = {n // 10 for n in numbers}
        assert len(decades) == 6, numbers
