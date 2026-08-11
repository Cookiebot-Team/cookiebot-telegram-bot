"""Step definitions for x_unearth.

QA: qa/features/x_unearth.feature (authored here — Cookiebot-QA has no scenario
for this feature; see the file's own header).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from cb_core import group_config, locales
from cb_gateway.handlers.unearth import pick_id
from qa.conftest import GROUP_ID, feed, make_message_update

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Iterator

    from aiogram import Bot, Dispatcher

    from qa.conftest import Context
    from qa.mock_telegram import MockTelegram

scenarios("x_unearth.feature")


@pytest.fixture(autouse=True)
def _reset_forward_failures(telegram: MockTelegram) -> Iterator[None]:
    """`forwardMessage` failures are per-scenario. Left set, the "every
    candidate is gone" scenario would silence every scenario after it."""
    yield
    telegram.clear_failures()


@given("that the bot is in the group and properly set up")
def bot_set_up(ctx: Context) -> None:
    ctx.bot_running = True


@given("that a user is in the group")
def user_in_group() -> None:
    pass


@given("that fun functions are disabled for the group")
def fun_disabled(database: Any, run: Callable[[Coroutine[Any, Any, Any]], Any]) -> None:
    """Takes the `database` fixture for the same reason `fun_random`'s does: the
    flag lives in `group_configs`, and that fixture skips the scenario cleanly
    when no Postgres is listening rather than failing it."""
    run(group_config.set_config(GROUP_ID, functions_fun=False))


@given("that the first message the bot tries to forward no longer exists")
def first_forward_fails(telegram: MockTelegram) -> None:
    """One miss, then success — the case v1's `return None` inside its own
    `except` made unreachable."""
    telegram.fail_times("forwardMessage", 1, "Bad Request: message to forward not found")


@given("that no message the bot tries to forward still exists")
def every_forward_fails(telegram: MockTelegram) -> None:
    telegram.fail("forwardMessage", "Bad Request: message to forward not found")


@when(parsers.parse('a user sends the command "{text}"'))
def user_sends(
    ctx: Context,
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
    text: str,
) -> None:
    feed(run, dispatcher, bot, make_message_update(text, ctx.update_id))


@then("the bot forwards a message from the same group")
def bot_forwards(telegram: MockTelegram) -> None:
    forwarded = telegram.calls_to("forwardMessage")
    assert forwarded, f"expected a forwardMessage call, got {telegram.calls}"
    last = forwarded[-1]
    # Source and destination are the same chat: v1 passes `chat_id` twice.
    assert int(last["chat_id"]) == GROUP_ID, last
    assert int(last["from_chat_id"]) == GROUP_ID, last


@then("the bot replies that fun functions are off")
def bot_says_fun_off(telegram: MockTelegram) -> None:
    sent = telegram.calls_to("sendMessage")
    assert sent, "expected a sendMessage call, got none"
    assert sent[-1].get("text", "") == locales.get("fun_off", "en")


@then("the user receives no response")
def no_response(telegram: MockTelegram) -> None:
    assert not telegram.calls_to("sendMessage"), telegram.calls


@when("the bot picks a candidate below message id 500")
def pick_candidate(ctx: Context) -> None:
    ctx.candidates = [pick_id(500) for _ in range(200)]  # type: ignore[attr-defined]


@then("the candidate is between 1 and 500")
def candidate_in_range(ctx: Context) -> None:
    candidates: list[int] = ctx.candidates  # type: ignore[attr-defined]
    assert all(1 <= candidate <= 500 for candidate in candidates), sorted(candidates)[:5]
