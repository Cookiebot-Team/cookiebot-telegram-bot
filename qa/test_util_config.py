"""Step definitions for util_config — the in-chat admin configuration menu.

Only the parts of the flow that succeed with no database are exercised here
(the read path degrades to `GroupConfig.DEFAULTS` with no Postgres, per
`cb_core.group_config.get_config`'s docstring). The write path — a button press
that actually changes a `group_configs` row — needs a real database and lives in
`qa/integration/test_config_menu.py` instead; see `docs/contracts/util_config.md`.

`handlers/__init__.py:build_router()` is another feature's file (out of scope for
this port — several features are being ported in parallel), so this module wires
its own `Dispatcher` around just `config_menu.router` rather than importing
`cb_gateway.main.dp`, which does not know about this handler yet.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Coroutine
from typing import Any

from aiogram import Bot, Dispatcher
from pytest_bdd import given, parsers, scenarios, then, when

from cb_gateway.handlers import config_menu
from qa.conftest import (
    ADMIN_ID,
    GROUP_ID,
    USER_ID,
    Context,
    feed,
    make_callback_update,
    make_message_update,
    make_private_message_update,
)
from qa.mock_telegram import MockTelegram

scenarios("util_config.feature")


# No local `dispatcher` fixture: the suite drives `cb_gateway.main.dp`, the
# dispatcher the service actually serves. A standalone one carrying only this
# router would stay green even if /config were unreachable in production.


def _dm_calls(telegram: MockTelegram) -> list[dict[str, Any]]:
    return [c for c in telegram.calls_to("sendMessage") if int(c.get("chat_id", 0)) == ADMIN_ID]


def _group_calls(telegram: MockTelegram) -> list[dict[str, Any]]:
    return [c for c in telegram.calls_to("sendMessage") if int(c.get("chat_id", 0)) == GROUP_ID]


def _set_full_admin(telegram: MockTelegram, user_id: int, chat_id: int = GROUP_ID) -> None:
    """Make `user_id` an administrator of `chat_id`.

    Thin wrapper over the shared mock, which now emits every field aiogram's
    `ChatMemberAdministrator` requires — the missing-fields gap this port found
    is fixed in `qa/mock_telegram.py` itself.
    """
    telegram.set_admins(chat_id, [(user_id, "administrator")])


@given(parsers.parse("the user sends the command {command}"))
def user_sends_command(ctx: Context, command: str) -> None:
    ctx.command_text = command


@given("the admin has opened the /config menu in their private chat")
def admin_opened_menu(
    ctx: Context,
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
    telegram: MockTelegram,
) -> None:
    _set_full_admin(telegram, ADMIN_ID)
    feed(run, dispatcher, bot, make_message_update("/config", ctx.update_id, user_id=ADMIN_ID))
    ctx.update_id += 1


@when("the user is an admin on that group")
def user_is_admin(
    ctx: Context,
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
    telegram: MockTelegram,
) -> None:
    _set_full_admin(telegram, ADMIN_ID)
    feed(
        run, dispatcher, bot, make_message_update(ctx.command_text, ctx.update_id, user_id=ADMIN_ID)
    )


@when("the user is not an admin on that group")
def user_is_not_admin(
    ctx: Context, run: Callable[[Coroutine[Any, Any, Any]], Any], dispatcher: Dispatcher, bot: Bot
) -> None:
    feed(
        run, dispatcher, bot, make_message_update(ctx.command_text, ctx.update_id, user_id=USER_ID)
    )


@when("the user is an anonymous admin on that group")
def user_is_anonymous_admin(
    ctx: Context, run: Callable[[Coroutine[Any, Any, Any]], Any], dispatcher: Dispatcher, bot: Bot
) -> None:
    feed(
        run,
        dispatcher,
        bot,
        make_message_update(ctx.command_text, ctx.update_id, anonymous=True),
    )


@when(parsers.parse('the admin presses the button for "{label}"'))
def admin_presses_button(
    ctx: Context,
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
    telegram: MockTelegram,
    label: str,
) -> None:
    dm_with_keyboard = [c for c in _dm_calls(telegram) if c.get("reply_markup")]
    assert dm_with_keyboard, "expected a DM containing the config menu keyboard"
    markup = json.loads(dm_with_keyboard[-1]["reply_markup"])
    callback_data = next(
        (
            button["callback_data"]
            for row in markup["inline_keyboard"]
            for button in row
            if button["text"] == label
        ),
        None,
    )
    assert callback_data, f"no menu button labeled {label!r}"
    ctx.update_id += 1
    ctx.callback_data = callback_data
    feed(
        run,
        dispatcher,
        bot,
        make_callback_update(callback_data, ctx.update_id, user_id=ADMIN_ID, chat_id=ADMIN_ID),
    )


@then("the bot should send a message on the group warning the admin to check their dms")
def warned_in_group(telegram: MockTelegram) -> None:
    calls = _group_calls(telegram)
    assert calls, "expected a group-facing sendMessage call"
    assert "private" in calls[-1].get("text", "").lower()


@then("the bot should send a message to the user dm's with the configuration options")
def sent_dm_with_options(telegram: MockTelegram) -> None:
    calls = _dm_calls(telegram)
    assert calls, "expected a DM to the admin"
    body = calls[-1].get("text", "")
    assert "Current settings:" in body
    assert calls[-1].get("reply_markup"), "expected the menu keyboard attached"


@then(parsers.parse('the bot should send a message on the group saying "{text}"'))
def group_message_says(telegram: MockTelegram, text: str) -> None:
    calls = _group_calls(telegram)
    assert calls, "expected a group-facing sendMessage call"
    assert text in calls[-1].get("text", "")


@then("display a video displaying how to remove anonymous mode from the user settings")
def tutorial_video_shown(telegram: MockTelegram) -> None:
    assert telegram.calls_to("sendVideo"), "expected the anonymous-mode tutorial video"


@then("the bot should not send the permission denied message")
def no_permission_denied(telegram: MockTelegram) -> None:
    denied_phrase = "You don't have permission to use this command or are in anonymous mode"
    for call in _group_calls(telegram):
        assert denied_phrase not in call.get("text", "")


@then("the bot should not display the anonymous mode tutorial video")
def no_tutorial_video(telegram: MockTelegram) -> None:
    assert not telegram.calls_to("sendVideo")


@then("the bot should tell the admin it could not reach them privately")
def cannot_reach_privately(telegram: MockTelegram) -> None:
    calls = _group_calls(telegram)
    assert calls, "expected a group-facing sendMessage call"
    assert "couldn't send" in calls[-1].get("text", "")


@then("the bot should answer the callback query")
def callback_answered(telegram: MockTelegram) -> None:
    assert telegram.calls_to("answerCallbackQuery"), "the callback was never answered (spinner bug)"


@then("the bot should prompt for the new value in the private chat")
def prompted_for_new_value(telegram: MockTelegram) -> None:
    calls = [c for c in telegram.calls_to("sendMessage") if int(c.get("chat_id", 0)) == ADMIN_ID]
    assert calls, "expected a prompt sent to the admin's private chat"
    assert "REPLY THIS MESSAGE with the new variable value" in calls[-1].get("text", "")


@then(parsers.parse('the button writes the "{column}" setting when answered'))
def button_writes_column(ctx: Context, column: str) -> None:
    """Ties the callback letter a button press just sent back to us to the
    `group_configs` column `config_menu.CONFIG_FIELDS` says it writes -- the
    per-button mapping this Scenario Outline exists to prove, without needing
    a database (the actual write is `qa/integration/test_config_menu.py`'s job,
    per this module's own docstring)."""
    assert ctx.callback_data is not None, "no button was pressed yet"
    parsed = config_menu.parse_callback_data(ctx.callback_data)
    assert parsed is not None, f"not a config callback: {ctx.callback_data!r}"
    letter, _group_id = parsed
    field = config_menu.FIELD_BY_LETTER[letter]
    assert field.column == column, (field.column, column)


# --- the language side effect (HANDOFF.md §4.3's gap) ------------------------


@given("the admin has been prompted for the new language")
def admin_prompted_for_language(
    ctx: Context,
    database: Any,
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
    telegram: MockTelegram,
) -> None:
    """Open the menu, then press the Language button, so the prompt this
    scenario replies to is the real one the handler recognises rather than a
    string retyped here.

    Takes `database` because this is the one scenario in this module that
    drives the *write* path — the module docstring's rule is that those need a
    real Postgres, and that fixture skips cleanly when there is none rather
    than failing offline. The side effect under test only fires after the write
    succeeds, which is exactly the ordering worth proving."""
    _set_full_admin(telegram, ADMIN_ID)
    feed(run, dispatcher, bot, make_message_update("/config", ctx.update_id, user_id=ADMIN_ID))
    ctx.update_id += 1
    field = config_menu.FIELD_BY_LETTER["k"]
    assert field.column == "language", field
    feed(
        run,
        dispatcher,
        bot,
        make_callback_update(
            config_menu.build_callback_data(field.letter, GROUP_ID),
            ctx.update_id,
            user_id=ADMIN_ID,
        ),
    )


@when(parsers.parse('the admin replies with "{value}"'))
def admin_replies_with(
    ctx: Context,
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
    telegram: MockTelegram,
    value: str,
) -> None:
    prompts = [c for c in telegram.calls_to("sendMessage") if int(c.get("chat_id", 0)) == ADMIN_ID]
    assert prompts, "the prompt was never sent, so there is nothing to reply to"
    update = make_private_message_update(value, ctx.update_id + 1, user_id=ADMIN_ID)
    update["message"]["reply_to_message"] = {
        "message_id": 9001,
        "date": 0,
        "chat": {"id": ADMIN_ID, "type": "private"},
        "from": {"id": 424242, "is_bot": True, "first_name": "Cookiebot"},
        "text": prompts[-1].get("text", ""),
    }
    feed(run, dispatcher, bot, update)


@then("the bot relabels the group's Telegram command menu")
def command_menu_relabelled(telegram: MockTelegram) -> None:
    """`setMyCommands`, scoped to the group — v1 relabels under all three
    Telegram UI languages (`setlang.set_group_commands`), so what matters here
    is that the call happened at all and named this chat."""
    calls = telegram.calls_to("setMyCommands")
    assert calls, f"the command menu was never relabelled: {telegram.calls}"
    scopes = [
        json.loads(c["scope"]) if isinstance(c.get("scope"), str) else c.get("scope") for c in calls
    ]
    assert any(int(scope.get("chat_id", 0)) == GROUP_ID for scope in scopes if scope), scopes
