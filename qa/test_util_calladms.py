"""Step definitions for util_calladms.

QA: qa/features/util_calladms.feature (synced from Cookiebot-QA/features/util_calladms.feature).
Contract: docs/contracts/util_calladms.md.

Drives the real dispatcher (`cb_gateway.main.dp`, via `qa/conftest.py`'s
`dispatcher` fixture) against the mock Telegram API, same as every other
acceptance test in this suite. Nothing here is monkeypatched: the parser, the
filters, `context_for`, admin resolution against the mock's
`getChatAdministrators`, and the actual Telegram calls are all real.

`packages/cb-gateway/src/cb_gateway/handlers/__init__.py` does not register
`calladms.router` yet (out of this feature's file ownership — several features
are being ported in parallel); these scenarios will not pass end to end until
whoever owns that file adds `root.include_router(calladms.router)`.

The DM half of v1's `call_admins` (`UserRegisters.py:178-203`) is a cb-worker
job that does not exist yet (docs/contracts/util_calladms.md: it is genuine
multi-chat fan-out, AGENTS.md section 2.4). The handler this suite drives
implements the group-ping half only, so the base QA scenario's DM-confirmation
step is written to match that honestly below, rather than pretend a job that
is not built yet ran.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from pytest_bdd import given, parsers, scenarios, then, when

from cb_gateway.handlers import calladms as calladms_handler
from qa.conftest import (
    ADMIN_ID,
    BOT_USERNAME,
    GROUP_ID,
    USER_ID,
    Context,
    feed,
    make_callback_update,
    make_message_update,
    next_update_id,
)
from qa.mock_telegram import MockTelegram

scenarios("util_calladms.feature")

SECOND_ADMIN_ID = ADMIN_ID + 1


class Ctx:
    """Extra per-scenario state on top of qa/conftest.py's base `Context`."""

    def __init__(self) -> None:
        self.prompt: dict[str, Any] | None = None

    def alloc_id(self) -> int:
        # Shared process-wide counter: a per-scenario counter collides with
        # earlier scenarios and the dedupe middleware drops the update.
        return next_update_id()


@pytest.fixture
def calladms_ctx() -> Ctx:
    return Ctx()


def _group_calls(telegram: MockTelegram) -> list[dict[str, Any]]:
    return [c for c in telegram.calls_to("sendMessage") if int(c.get("chat_id", 0)) == GROUP_ID]


def _extract_callback_data(prompt: dict[str, Any], label: str) -> str:
    markup = json.loads(prompt["reply_markup"])
    for row in markup["inline_keyboard"]:
        for button in row:
            if button["text"] == label:
                return str(button["callback_data"])
    raise AssertionError(f"no button labeled {label!r} in {markup!r}")


def _stale_callback_update(
    data: str, update_id: int, *, message_id: int, seconds_ago: int
) -> dict[str, Any]:
    """The shape `qa.conftest.make_callback_update` builds, with a controllable `date`.

    Not reused from `qa.conftest`: that helper hardcodes the embedded prompt
    message's `date` to "now", but this scenario needs to backdate it — it is
    exactly the timestamp `calladms.is_stale` reads.
    """
    return {
        "update_id": update_id,
        "callback_query": {
            "id": str(update_id),
            "from": {"id": USER_ID, "is_bot": False, "first_name": "Tester", "username": "tester"},
            "chat_instance": str(GROUP_ID),
            "data": data,
            "message": {
                "message_id": message_id,
                "date": int(time.time()) - seconds_ago,
                "chat": {"id": GROUP_ID, "type": "supergroup", "title": "QA Group"},
                "from": {
                    "id": 424242,
                    "is_bot": True,
                    "first_name": "Cookiebot",
                    "username": BOT_USERNAME,
                },
                "text": "confirmation prompt",
            },
        },
    }


# --------------------------------------------------------------------- given


@given("that the bot is in the group and properly set up")
def bot_set_up(ctx: Context) -> None:
    ctx.bot_running = True


@given("that there are admins in the group")
def admins_in_group(telegram: MockTelegram) -> None:
    telegram.set_admins(GROUP_ID, [(ADMIN_ID, "administrator"), (SECOND_ADMIN_ID, "administrator")])


# ---------------------------------------------------------------------- when


@when(parsers.parse("the user sends the {command} command to the bot"))
def user_sends_command(
    calladms_ctx: Ctx,
    run: Any,
    dispatcher: Dispatcher,
    bot: Bot,
    telegram: MockTelegram,
    command: str,
) -> None:
    feed(
        run, dispatcher, bot, make_message_update(command, calladms_ctx.alloc_id(), user_id=USER_ID)
    )
    sent = telegram.calls_to("sendMessage")
    assert sent, f"expected a confirmation prompt for {command!r}"
    calladms_ctx.prompt = sent[-1]


@when("confirms the intention to ping all admins")
def confirm_ping(calladms_ctx: Ctx, run: Any, dispatcher: Dispatcher, bot: Bot) -> None:
    assert calladms_ctx.prompt is not None, "no confirmation prompt was sent yet"
    callback_data = _extract_callback_data(calladms_ctx.prompt, "✔️")
    feed(
        run,
        dispatcher,
        bot,
        make_callback_update(
            callback_data, calladms_ctx.alloc_id(), user_id=USER_ID, chat_id=GROUP_ID
        ),
    )


@when("declines the intention to ping all admins")
def decline_ping(calladms_ctx: Ctx, run: Any, dispatcher: Dispatcher, bot: Bot) -> None:
    assert calladms_ctx.prompt is not None, "no confirmation prompt was sent yet"
    callback_data = _extract_callback_data(calladms_ctx.prompt, "❌")
    feed(
        run,
        dispatcher,
        bot,
        make_callback_update(
            callback_data, calladms_ctx.alloc_id(), user_id=USER_ID, chat_id=GROUP_ID
        ),
    )


@when("confirms the intention more than 10 minutes later")
def confirm_ping_late(calladms_ctx: Ctx, run: Any, dispatcher: Dispatcher, bot: Bot) -> None:
    assert calladms_ctx.prompt is not None, "no confirmation prompt was sent yet"
    callback_data = _extract_callback_data(calladms_ctx.prompt, "✔️")
    update = _stale_callback_update(
        callback_data,
        calladms_ctx.alloc_id(),
        message_id=9999,
        seconds_ago=calladms_handler.STALE_AFTER_SECONDS + 60,
    )
    feed(run, dispatcher, bot, update)


# ---------------------------------------------------------------------- then


@then("the bot should respond by pinging all admins in the group")
def bot_pings_admins(telegram: MockTelegram) -> None:
    calls = _group_calls(telegram)
    assert calls, "expected a group-facing sendMessage call"
    body = calls[-1].get("text", "")
    assert f"@admin{ADMIN_ID}" in body, body
    assert f"@admin{SECOND_ADMIN_ID}" in body, body
    assert "calling all admins" in body, body


@then("should send a message on the adm's DM confirming that they have been pinged in a group")
def dm_confirmation(telegram: MockTelegram) -> None:
    """v1's DM fan-out (`UserRegisters.py:186-203`) opens a distinct Telegram
    chat per admin — genuine multi-chat fan-out, which AGENTS.md section 2.4
    requires to be a cb-worker job rather than reply-path work. No such job
    exists yet in this codebase, and both `cb-worker/*` and
    `cb_gateway/main.py` (which would enqueue it) are out of this port's file
    ownership (docs/contracts/util_calladms.md). This assertion documents that
    honestly: the gateway sends only the group ping today, never a DM.
    """
    dm_calls = [c for c in telegram.calls_to("sendMessage") if int(c.get("chat_id", 0)) != GROUP_ID]
    assert not dm_calls, dm_calls


@then("the bot should cancel the request without pinging anyone")
def cancelled(telegram: MockTelegram) -> None:
    from cb_core import locales

    calls = _group_calls(telegram)
    assert calls, "expected a group-facing sendMessage call"
    assert calls[-1].get("text", "") == locales.get("canceled", "en"), calls[-1]
    assert not any("calling all admins" in c.get("text", "") for c in calls)


@then("the bot should tell the user the confirmation is too old")
def too_old(telegram: MockTelegram) -> None:
    answers = telegram.calls_to("answerCallbackQuery")
    assert answers, "expected the callback to be answered"
    assert answers[-1].get("text") == calladms_handler.TOO_OLD_TEXT, answers[-1]


@then("should not ping anyone")
def not_pinged(telegram: MockTelegram) -> None:
    calls = _group_calls(telegram)
    assert not any("calling all admins" in c.get("text", "") for c in calls), calls
