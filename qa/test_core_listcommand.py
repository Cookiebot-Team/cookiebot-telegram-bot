"""Step definitions for core_listcommand.

QA: qa/features/core_listcommand.feature (synced from
Cookiebot-QA/features/core_listcommand.feature).
Contract: docs/contracts/core_listcommand.md.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Coroutine
from typing import Any

from aiogram import Bot, Dispatcher
from pytest_bdd import given, parsers, scenarios, then, when

from qa.conftest import USER_ID, Context, feed, make_message_update
from qa.mock_telegram import MockTelegram

scenarios("core_listcommand.feature")


# No local `dispatcher` fixture: this suite drives `cb_gateway.main.dp`, the
# dispatcher the service actually serves, middlewares and registration included.
# A standalone Dispatcher carrying only this router would pass while the handler
# was unreachable in production — which is the one thing an acceptance test is
# supposed to catch (AGENTS.md §6).


# The v1 help text every scenario looks for — the first line of
# cb_core/locale_data/en/Cookiebot_functions.txt, ported byte-identical from
# Bot/Static/locales/eng/Cookiebot_functions.txt.
_HELP_MARKER = "Cookiebot Features!"


def _make_private_update(text: str, update_id: int, *, user_id: int = USER_ID) -> dict[str, Any]:
    """A DM update. `qa.conftest.make_message_update` always builds a supergroup
    chat, so this is its private-chat counterpart, built the same way but not
    added there (this feature's file ownership does not include qa/conftest.py).
    """
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": int(time.time()),
            "chat": {"id": user_id, "type": "private", "first_name": "Tester"},
            "from": {"id": user_id, "is_bot": False, "first_name": "Tester", "username": "tester"},
            "text": text,
            "entities": [{"offset": 0, "length": len(text.split(" ")[0]), "type": "bot_command"}]
            if text.startswith("/")
            else [],
        },
    }


@given("that the bot is online and operational")
def bot_online(ctx: Context) -> None:
    ctx.bot_running = True


@given("that the user is a member of the group")
def user_in_group() -> None:
    pass


@given("that the user is not a member of any group")
def user_not_in_group() -> None:
    pass


@when(parsers.parse("they type {text} in the group chat"))
def user_types_in_group(
    ctx: Context,
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
    text: str,
) -> None:
    feed(run, dispatcher, bot, make_message_update(text, ctx.update_id))


@when(parsers.parse("they type {text} in a private chat with the bot"))
def user_types_in_private(
    ctx: Context,
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
    text: str,
) -> None:
    feed(run, dispatcher, bot, _make_private_update(text, ctx.update_id))


@then("they should see a list of commands available to them")
def sees_command_list(telegram: MockTelegram) -> None:
    sent = telegram.calls_to("sendMessage")
    assert sent, "expected a sendMessage call, got none"
    body = sent[-1].get("text", "")
    assert _HELP_MARKER in body, body


@then("the user receives no response")
def no_response(telegram: MockTelegram) -> None:
    assert not telegram.calls_to("sendMessage"), telegram.calls
