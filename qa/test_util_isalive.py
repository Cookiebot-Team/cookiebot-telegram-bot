"""Step definitions for util_isalive — M0's acceptance gate.

Proves the full ingest path end to end: update -> dedupe -> telemetry ->
compiled parser -> filter -> handler -> Telegram API call.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pytest_bdd import given, parsers, scenarios, then, when

from qa.conftest import feed, make_message_update

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from aiogram import Bot, Dispatcher

    from qa.conftest import Context
    from qa.mock_telegram import MockTelegram

scenarios("util_isalive.feature")


@given("the bot is set up in the group")
def bot_set_up(ctx: Context) -> None:
    ctx.bot_running = True


@given("the bot is running and responsive")
def bot_running(ctx: Context) -> None:
    ctx.bot_running = True


@given("the bot is not running")
def bot_not_running(ctx: Context) -> None:
    # Nothing consumes the update: modelling a dead process, exactly as the spec
    # describes it ("the user receives no response").
    ctx.bot_running = False


@when(parsers.parse('the user sends "{text}"'))
def user_sends(
    ctx: Context,
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
    text: str,
) -> None:
    if not ctx.bot_running:
        return
    feed(run, dispatcher, bot, make_message_update(text, ctx.update_id))


@then("the bot replies confirming it is alive and operational")
def bot_confirms_alive(telegram: MockTelegram) -> None:
    sent = telegram.calls_to("sendMessage")
    assert sent, "expected a sendMessage call, got none"
    body = sent[-1].get("text", "")
    assert "Alive and operational" in body, body
    assert "uptime" in body


@then("the user receives no response")
def no_response(telegram: MockTelegram) -> None:
    assert not telegram.calls_to("sendMessage"), telegram.calls
