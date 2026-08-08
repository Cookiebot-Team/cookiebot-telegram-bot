"""Step definitions for x_giveaways.

QA: `qa/features/x_giveaways.feature` — **authored, not ported**. The QA repo
has no giveaway feature file; every step here is derived from
`../COOKIEBOT-Telegram-Group-Bot/Bot/Giveaways.py` and its dispatch.
Contract: `docs/contracts/x_giveaways.md`.

Drives the real dispatcher against the mock Telegram API. Nothing of ours is
mocked: the parser, the filters, `context_for`, admin resolution against the
mock's `getChatAdministrators`, the Valkey-backed pending prize and the real
`giveaways` tables are all exercised. That is why this suite asks for both the
`database` and `valkey` fixtures — a giveaway *is* its rows, and a handler that
invented a reply with no store behind it would be worse than one that stays
quiet (`qa/conftest.py`'s `database` docstring).

The announcement's `message_id` is assigned by Telegram (here, the mock), so a
button press has to address the message the bot actually sent. The step reads
it back out of the `giveaways` row rather than guessing — the same id the
handler wrote, which is the point.
"""

from __future__ import annotations

import json
from types import ModuleType
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from pytest_bdd import given, parsers, scenarios, then, when

from qa.conftest import (
    ADMIN_ID,
    GROUP_ID,
    USER_ID,
    Context,
    feed,
    make_callback_update,
    make_message_update,
    next_update_id,
)
from qa.mock_telegram import MockTelegram

scenarios("x_giveaways.feature")


@pytest.fixture(autouse=True)
def _giveaway_infra(database: ModuleType, valkey: ModuleType, clean_giveaways: None) -> None:
    """Both stores, and a clean `giveaways` table, for every scenario here."""


def _sent(telegram: MockTelegram) -> list[dict[str, Any]]:
    return [
        call for call in telegram.calls_to("sendMessage") if int(call.get("chat_id", 0)) == GROUP_ID
    ]


def _keyboard(call: dict[str, Any]) -> list[list[dict[str, Any]]]:
    markup = call.get("reply_markup")
    assert markup, f"expected an inline keyboard on {call!r}"
    rows: list[list[dict[str, Any]]] = json.loads(markup)["inline_keyboard"]
    return rows


def _callback_data_for(call: dict[str, Any], label: str) -> str:
    for row in _keyboard(call):
        for button in row:
            if button["text"] == label:
                return str(button["callback_data"])
    raise AssertionError(f"no button labelled {label!r} in {call!r}")


def _press(
    run: Any,
    dispatcher: Dispatcher,
    bot: Bot,
    data: str,
    *,
    user_id: int,
    message_id: int,
) -> None:
    feed(
        run,
        dispatcher,
        bot,
        make_callback_update(
            data, next_update_id(), user_id=user_id, chat_id=GROUP_ID, message_id=message_id
        ),
    )


def _live_message_id(run: Any) -> int:
    """The message the group's one live giveaway currently hangs off.

    Not the announcement's id forever: `end` re-points the row at the "draw
    more winners?" message it posts (v1 `Giveaways.py:156`), and a press after
    that has to address *that* message.
    """
    from cb_core import db

    row = run(
        db.fetchrow(
            "SELECT message_id FROM giveaways WHERE group_id = $1 ORDER BY created_at DESC LIMIT 1",
            GROUP_ID,
            name="qa_giveaway_message_id",
        )
    )
    assert row is not None, "no giveaway row exists"
    return int(row["message_id"])


# ---------------------------------------------------------------------- given


@given("that the bot is in the group and properly set up")
def bot_set_up(ctx: Context) -> None:
    ctx.bot_running = True


@given("that an admin is in the group")
def admin_in_group(telegram: MockTelegram) -> None:
    telegram.set_admins(GROUP_ID, [(ADMIN_ID, "administrator")])


@given("that the user is in the group")
def user_in_group() -> None:
    pass


@given("utility functions are disabled for the group")
def utility_off(run: Any) -> None:
    from cb_core import group_config

    run(group_config.set_config(GROUP_ID, functions_utility=False))


@given(parsers.parse('a giveaway for "{prize}" with {winners:d} winner is running'))
def giveaway_running(
    run: Any, dispatcher: Dispatcher, bot: Bot, telegram: MockTelegram, prize: str, winners: int
) -> None:
    feed(
        run,
        dispatcher,
        bot,
        make_message_update(f"/giveaway {prize}", next_update_id(), user_id=ADMIN_ID),
    )
    prompt = _sent(telegram)[-1]
    _press(
        run,
        dispatcher,
        bot,
        _callback_data_for(prompt, str(winners)),
        user_id=ADMIN_ID,
        message_id=next_update_id(),
    )
    assert _live_message_id(run)


@given("the user has entered the giveaway")
def user_entered(run: Any, dispatcher: Dispatcher, bot: Bot, telegram: MockTelegram) -> None:
    press_enter(run, dispatcher, bot, telegram)


# ----------------------------------------------------------------------- when


@when(parsers.parse('the admin sends the command "{text}"'))
def admin_sends(run: Any, dispatcher: Dispatcher, bot: Bot, text: str) -> None:
    feed(run, dispatcher, bot, make_message_update(text, next_update_id(), user_id=ADMIN_ID))


@when(parsers.parse('the user sends the command "{text}"'))
def user_sends(run: Any, dispatcher: Dispatcher, bot: Bot, text: str) -> None:
    feed(run, dispatcher, bot, make_message_update(text, next_update_id(), user_id=USER_ID))


@when(parsers.parse("picks {winners:d} winners"))
def picks_winners(
    run: Any, dispatcher: Dispatcher, bot: Bot, telegram: MockTelegram, winners: int
) -> None:
    prompt = _sent(telegram)[-1]
    _press(
        run,
        dispatcher,
        bot,
        _callback_data_for(prompt, str(winners)),
        user_id=ADMIN_ID,
        message_id=next_update_id(),
    )


@when("the user presses the enter button")
def press_enter(run: Any, dispatcher: Dispatcher, bot: Bot, telegram: MockTelegram) -> None:
    _press(
        run,
        dispatcher,
        bot,
        "GIVEAWAY enter",
        user_id=USER_ID,
        message_id=_live_message_id(run),
    )


@when("the user presses the enter button again")
def press_enter_again(run: Any, dispatcher: Dispatcher, bot: Bot, telegram: MockTelegram) -> None:
    press_enter(run, dispatcher, bot, telegram)


@when("the admin presses the end button")
def press_end_as_admin(run: Any, dispatcher: Dispatcher, bot: Bot) -> None:
    _press(run, dispatcher, bot, "GIVEAWAY end", user_id=ADMIN_ID, message_id=_live_message_id(run))


@when("the user presses the end button")
def press_end_as_member(run: Any, dispatcher: Dispatcher, bot: Bot) -> None:
    _press(run, dispatcher, bot, "GIVEAWAY end", user_id=USER_ID, message_id=_live_message_id(run))


# ----------------------------------------------------------------------- then


@then("the bot should ask how many users will be drawn")
def asks_the_count(telegram: MockTelegram) -> None:
    assert "How many users will be drawn?" in _sent(telegram)[-1]["text"]


@then("should offer one button per winner count from 1 to 5")
def offers_five_counts(telegram: MockTelegram) -> None:
    rows = _keyboard(_sent(telegram)[-1])
    assert [button["text"] for (button,) in rows] == ["1", "2", "3", "4", "5"]


@then("the bot should say they do not have permission")
def says_no_permission(telegram: MockTelegram) -> None:
    assert "don't have permission to create giveaways" in _sent(telegram)[-1]["text"]


@then("the bot should say what is being raffled must be typed")
def says_type_the_prize(telegram: MockTelegram) -> None:
    assert "need to type what is being raffled" in _sent(telegram)[-1]["text"]


@then("the bot should announce the giveaway naming the full prize")
def announces_the_giveaway(telegram: MockTelegram) -> None:
    body = _sent(telegram)[-1]["text"]
    assert "IT'S GIVEAWAY TIME!" in body
    # The whole prize, not v1's 20-character `json.dumps` slice (D-GA-1).
    assert "Fursuit of Mekhy" in body


@then("should pin the announcement")
def pins_it(telegram: MockTelegram) -> None:
    assert telegram.calls_to("pinChatMessage"), "the announcement was never pinned"


@then("should offer an enter button and an end button")
def offers_entry_buttons(telegram: MockTelegram) -> None:
    rows = _keyboard(_sent(telegram)[-1])
    assert [button["callback_data"] for (button,) in rows] == [
        "GIVEAWAY enter",
        "GIVEAWAY end",
    ]


def _answers(telegram: MockTelegram) -> list[str]:
    return [str(call.get("text", "")) for call in telegram.calls_to("answerCallbackQuery")]


@then("the bot should confirm they entered the giveaway")
def confirms_entry(telegram: MockTelegram) -> None:
    assert "YAY! You entered the giveaway!" in _answers(telegram)


@then("the bot should say they are already participating")
def already_in(telegram: MockTelegram) -> None:
    assert _answers(telegram)[-1] == "You are already participating!"


@then("the bot should say there were no participants")
def no_participants(telegram: MockTelegram) -> None:
    assert "No participants in the giveaway!" in [
        str(call.get("text", "")) for call in _sent(telegram)
    ]


@then("the bot should announce the winner with the prize")
def announces_the_winner(telegram: MockTelegram) -> None:
    bodies = [str(call.get("text", "")) for call in _sent(telegram)]
    captions = [str(call.get("caption", "")) for call in telegram.calls_to("sendPhoto")]
    assert any(
        "We have a winner!" in body and "Fursuit of Mekhy" in body for body in bodies + captions
    ), bodies + captions


@then("should ask whether to draw more winners")
def asks_to_draw_more(telegram: MockTelegram) -> None:
    last = _sent(telegram)[-1]
    assert last["text"] == "Draw more winners?"
    rows = _keyboard(last)
    assert [button["callback_data"] for (button,) in rows] == [
        "GIVEAWAY end",
        "GIVEAWAY delete",
    ]


@then("the bot should say only admins can end it")
def only_admins_end(telegram: MockTelegram) -> None:
    assert _answers(telegram)[-1] == "Only admins can end!"


@then("the bot should say utility functions are off")
def utility_is_off(telegram: MockTelegram) -> None:
    from cb_core import locales

    assert _sent(telegram)[-1]["text"] == locales.get("utility_off", "en")
