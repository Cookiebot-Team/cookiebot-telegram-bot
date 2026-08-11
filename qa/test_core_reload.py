"""Step definitions for core_reload.

QA: qa/features/core_reload.feature (authored here — see the file's own header).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from cb_core import locales
from qa.conftest import feed, make_message_update

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Iterator

    from aiogram import Bot, Dispatcher

    from qa.conftest import Context
    from qa.mock_telegram import MockTelegram

scenarios("core_reload.feature")


@pytest.fixture(autouse=True)
def _clear_failures(telegram: MockTelegram) -> Iterator[None]:
    yield
    telegram.clear_failures()


@given("that the bot is in the group and properly set up")
def bot_set_up(ctx: Context) -> None:
    ctx.bot_running = True


@given("that a user is in the group")
def user_in_group() -> None:
    pass


@given("that Telegram will not return the group's administrators")
def admins_unavailable(telegram: MockTelegram) -> None:
    """The half of the refresh that can fail. v1 would have thrown here and
    answered nothing; the handler logs and still confirms, because the caller
    asked for a refresh and half of it happened."""
    telegram.fail("getChatAdministrators", "Bad Request: chat not found")


@when(parsers.parse('a user sends the command "{text}"'))
def user_sends(
    ctx: Context,
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
    text: str,
) -> None:
    feed(run, dispatcher, bot, make_message_update(text, ctx.update_id))


@then("the bot confirms the memory was reloaded")
def bot_confirms(telegram: MockTelegram) -> None:
    sent = telegram.calls_to("sendMessage")
    assert sent, f"expected a sendMessage call, got {telegram.calls}"
    assert sent[-1].get("text", "") == locales.get("reload", "en")
