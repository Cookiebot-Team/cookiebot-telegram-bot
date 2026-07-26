"""Step definitions for core_privacy.

QA: qa/features/core_privacy.feature (synced from Cookiebot-QA/features/core_privacy.feature).
Contract: docs/contracts/core_privacy.md.
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

scenarios("core_privacy.feature")


@given("that the bot is in the group and properly set up")
def bot_set_up(ctx: Context) -> None:
    ctx.bot_running = True


@given("that a user is in the group")
def user_in_group() -> None:
    pass


@when(parsers.parse('a user sends the command "{text}"'))
def user_sends(
    ctx: Context,
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
    text: str,
) -> None:
    feed(run, dispatcher, bot, make_message_update(text, ctx.update_id))


@then("the bot should reply with a message containing the privacy politics of the bot")
def bot_replies_with_privacy_text(telegram: MockTelegram) -> None:
    sent = telegram.calls_to("sendMessage")
    assert sent, "expected a sendMessage call, got none"
    body = sent[-1].get("text", "")
    assert "https://cookiebotfur.net/privacy" in body, body


@then("the user receives no response")
def no_response(telegram: MockTelegram) -> None:
    assert not telegram.calls_to("sendMessage"), telegram.calls
