"""Step definitions for core_welcome.

QA: qa/features/core_welcome.feature (synced from Cookiebot-QA/features/core_welcome.feature).
Contract: docs/contracts/core_welcome.md.

Drives the real dispatcher (`cb_gateway.main.dp`, via `qa/conftest.py`'s
`dispatcher` fixture) against the mock Telegram API, same as every other
acceptance test in this suite. The one thing faked here is the
`group_welcomes` table itself: `cb_gateway.handlers.welcome._fetch_welcome_body`
/ `_save_welcome` are the DB seam this handler owns, and this suite runs
offline (no Postgres — see `qa/conftest.py`'s module docstring), so they are
monkeypatched to an in-process dict for the duration of each scenario.
Everything else — parsing, filters, `context_for`, admin resolution against
the mock's `getChatAdministrators`, the actual Telegram calls — is real. The
real Postgres round trip for `group_welcomes` is covered separately by
`qa/integration/test_group_welcomes.py`.

NOTE: `packages/cb-gateway/src/cb_gateway/handlers/__init__.py` does not
register `welcome.router` yet (out of this feature's file ownership — see the
task's file list; `rules.router` and `config_menu.router` aren't registered
there yet either, so this port is not alone). These scenarios will not pass end
to end until whoever owns that file adds `root.include_router(welcome.router)`.

Also see docs/contracts/core_welcome.md's "QA vs. v1 conflict" section for why
the "not an admin" scenario's step definitions below drive a different trigger
point than its literal wording describes.
"""

from __future__ import annotations

import html
import time
from typing import TYPE_CHECKING, Any

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from cb_core import locales
from cb_gateway.handlers import welcome as welcome_handler
from qa.conftest import (
    ADMIN_ID,
    GROUP_ID,
    NEWCOMER_ID,
    USER_ID,
    feed,
    make_join_update,
    make_message_update,
    next_update_id,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from aiogram import Bot, Dispatcher

    from qa.conftest import Context
    from qa.mock_telegram import MockTelegram

scenarios("core_welcome.feature")


@pytest.fixture(autouse=True)
def _real_welcome_table(clean_welcomes: None) -> None:
    """The real `group_welcomes` table, truncated for this group around each scenario.

    Not a monkeypatched seam: the welcome a member sees *is* the stored row, so
    faking storage would leave the scenario asserting that the handler can read
    back a dict it just wrote. AGENTS.md §6 forbids mocking our own code in an
    acceptance test; `clean_welcomes` skips the suite when no database is up.
    """


class Ctx:
    """Extra per-scenario state on top of qa/conftest.py's base `Context`."""

    def __init__(self) -> None:
        self.sender_id = USER_ID
        self.new_welcome_prompt: dict[str, Any] | None = None
        self.submitted_text: str | None = None

    def alloc_id(self) -> int:
        # Process-wide: a per-scenario counter repeats ids the dedupe middleware
        # has already seen, and the update is dropped as a redelivery.
        return next_update_id()


@pytest.fixture
def welcome_ctx() -> Ctx:
    return Ctx()


def _reply_to_prompt(welcome_ctx: Ctx) -> dict[str, Any]:
    assert welcome_ctx.new_welcome_prompt is not None, "no /newwelcome prompt was sent yet"
    return welcome_ctx.new_welcome_prompt


def _join_update(
    update_id: int,
    joiners: list[dict[str, Any]],
    *,
    chat_id: int = GROUP_ID,
    by_user_id: int = USER_ID,
) -> dict[str, Any]:
    """Like `qa.conftest.make_join_update`, but each joiner is given verbatim
    (`is_bot`, a custom `id`, no `username`) — needed for the "another bot" /
    "the bot itself" / "no username" scenarios below, which conftest.py's
    helper (out of this task's file ownership; its `_user()` always sets
    `is_bot: False` and always assigns a username) cannot express.
    """
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": int(time.time()),
            "chat": {"id": chat_id, "type": "supergroup", "title": "QA Group"},
            "from": {
                "id": by_user_id,
                "is_bot": False,
                "first_name": "Tester",
                "username": "tester",
            },
            "new_chat_members": joiners,
        },
    }


# --------------------------------------------------------------------- given


@given("that the bot is in the group and properly set up")
def bot_set_up(ctx: Context) -> None:
    ctx.bot_running = True


@given(parsers.parse("the user sends the command {command}"))
def user_sends_command(
    welcome_ctx: Ctx,
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
    telegram: MockTelegram,
    command: str,
) -> None:
    update_id = welcome_ctx.alloc_id()
    feed(
        run, dispatcher, bot, make_message_update(command, update_id, user_id=welcome_ctx.sender_id)
    )
    if command.split("@")[0] in ("/newwelcome", "/novobemvindo", "/nuevabienvenida"):
        # The mock records the raw request payload, not the response Telegram
        # would hand back, so the prompt's own message id is fabricated — the
        # handler only ever compares `reply_to_message.text`, never its id.
        #
        # The text is unescaped for the same reason: what goes out is
        # `WELCOME_PROMPT_HTML` (`<user>` escaped, or Telegram rejects the whole
        # send), and what Telegram puts in `reply_to_message.text` is the
        # rendered form. The mock parses no entities, so this does that step by
        # hand — otherwise the fabricated prompt carries `&lt;user&gt;`, which is
        # not what a real reply would quote back.
        sent = telegram.calls_to("sendMessage")[-1]
        welcome_ctx.new_welcome_prompt = {
            "message_id": update_id,
            "date": sent.get("date", 0) or 0,
            "chat": {"id": GROUP_ID, "type": "supergroup", "title": "QA Group"},
            "from": {
                "id": 424242,
                "is_bot": True,
                "first_name": "Cookiebot",
                "username": "CookieMWbot",
            },
            "text": html.unescape(sent.get("text", "")),
        }


@given("a new member joins the group")
def new_member_joins(
    welcome_ctx: Ctx,
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
) -> None:
    update_id = welcome_ctx.alloc_id()
    feed(run, dispatcher, bot, make_join_update(update_id, joiners=[(NEWCOMER_ID, "Newcomer")]))


@given(parsers.parse('the group\'s welcome message is set to "{text}"'))
def welcome_message_is_set(text: str, run: Callable[[Coroutine[Any, Any, Any]], Any]) -> None:
    run(welcome_handler._save_welcome(GROUP_ID, ADMIN_ID, text))  # noqa: SLF001


@given("a new member without a Telegram username joins the group")
def newcomer_without_username_joins(
    welcome_ctx: Ctx,
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
) -> None:
    update_id = welcome_ctx.alloc_id()
    feed(
        run,
        dispatcher,
        bot,
        _join_update(update_id, [{"id": NEWCOMER_ID, "is_bot": False, "first_name": "Alex"}]),
    )


# The feature writes this as a `Then ... And`, so it must be registered as a
# `then` step — pytest-bdd matches on step type, and a `given` of the same text
# is simply not found.
@then("the admin should be able to reply to the bot's message with the new welcome message")
def admin_replies_with_new_welcome(
    welcome_ctx: Ctx,
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
) -> None:
    welcome_ctx.submitted_text = "Welcome aboard, <user>!"
    feed(
        run,
        dispatcher,
        bot,
        make_message_update(
            welcome_ctx.submitted_text,
            welcome_ctx.alloc_id(),
            user_id=welcome_ctx.sender_id,
            reply_to=_reply_to_prompt(welcome_ctx),
        ),
    )


@given("a bot account joins the group")
def bot_account_joins(
    welcome_ctx: Ctx,
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
) -> None:
    update_id = welcome_ctx.alloc_id()
    feed(
        run,
        dispatcher,
        bot,
        _join_update(
            update_id,
            [{"id": 900_001, "is_bot": True, "first_name": "OtherBot", "username": "otherbot"}],
        ),
    )


@given("the bot itself is added as a new member")
def bot_itself_added(
    welcome_ctx: Ctx,
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
) -> None:
    update_id = welcome_ctx.alloc_id()
    feed(
        run,
        dispatcher,
        bot,
        # id 424242 matches TEST_TOKEN's prefix (qa/conftest.py) -> Bot.id.
        _join_update(
            update_id,
            [{"id": 424242, "is_bot": True, "first_name": "Cookiebot", "username": "CookieMWbot"}],
        ),
    )


@given("three new members join the group in the same update")
def three_join_at_once(
    welcome_ctx: Ctx,
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
) -> None:
    run(welcome_handler._save_welcome(GROUP_ID, ADMIN_ID, "Welcome <user>!"))  # noqa: SLF001
    update_id = welcome_ctx.alloc_id()
    feed(
        run,
        dispatcher,
        bot,
        _join_update(
            update_id,
            [
                {"id": NEWCOMER_ID, "is_bot": False, "first_name": "First", "username": "first"},
                {
                    "id": NEWCOMER_ID + 1,
                    "is_bot": False,
                    "first_name": "Second",
                    "username": "second",
                },
                {
                    "id": NEWCOMER_ID + 2,
                    "is_bot": False,
                    "first_name": "Third",
                    "username": "third",
                },
            ],
        ),
    )


# ---------------------------------------------------------------------- when


@when("the bot detects that a new member has joined")
def bot_detects_join() -> None:
    """The Given step above already fed the update; nothing further to do."""


@when("the user is an admin on that group")
def user_is_admin(welcome_ctx: Ctx, telegram: MockTelegram) -> None:
    telegram.set_admins(GROUP_ID, [(welcome_ctx.sender_id, "administrator")])


@when("the user is not an admin on that group")
def user_is_not_admin(welcome_ctx: Ctx, telegram: MockTelegram) -> None:
    telegram.set_admins(GROUP_ID, [(ADMIN_ID, "administrator")])
    assert welcome_ctx.sender_id != ADMIN_ID


@when("a user who is not an admin on that group replies to the bot's prompt with new welcome text")
def non_admin_replies(
    welcome_ctx: Ctx,
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
    telegram: MockTelegram,
) -> None:
    telegram.set_admins(GROUP_ID, [(ADMIN_ID, "administrator")])
    welcome_ctx.submitted_text = "No welcome, whatever."
    feed(
        run,
        dispatcher,
        bot,
        make_message_update(
            welcome_ctx.submitted_text,
            welcome_ctx.alloc_id(),
            user_id=USER_ID,
            reply_to=_reply_to_prompt(welcome_ctx),
        ),
    )


# ---------------------------------------------------------------------- then


@then(parsers.parse('the bot should display the message "{text}"'))
def bot_displays_message(telegram: MockTelegram, text: str) -> None:
    sent = telegram.calls_to("sendMessage")
    assert sent, "expected a sendMessage call, got none"
    # The scenario says what the admin *reads*; this assertion sees what went on
    # the wire, and for this prompt the two differ. It contains a literal
    # `<user>` — the placeholder it is telling the admin to type — and the bot
    # sends `parse_mode=HTML`, which rejects `<user>` as an unsupported start
    # tag and failed the whole command in UAT (`welcome.py:WELCOME_PROMPT_HTML`).
    # So it goes out escaped and Telegram renders it back. Unescaping here
    # compares the two things that are meant to be equal. This mock does no
    # entity parsing of its own, which is exactly why it never caught that.
    on_the_wire = html.unescape(sent[-1].get("text", ""))
    # v1's real prompt (Configurations.py:267) contains a literal "\n\n" that
    # Gherkin's single-line string cannot represent; the spec text collapses
    # it to a single space.
    assert on_the_wire.replace("\n\n", " ") == text, sent[-1]


@then(parsers.parse('the bot should send a message on the group saying "{text}"'))
def bot_says_on_group(
    welcome_ctx: Ctx,
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
    telegram: MockTelegram,
    text: str,
) -> None:
    """For most quoted strings this is a plain equality check on the last
    `sendMessage`. One string is special-cased: see the docstring on the
    branch below.
    """
    if text == "You don't have permission to use this command or are in anonymous mode":
        # docs/contracts/core_welcome.md, "QA vs. v1 conflict": this exact
        # text (and the video in the next step) belong to /configurar's
        # anonymous-admin defect, not to /newwelcome. v1's real /newwelcome
        # has no admin check on the bare command — the rejection only fires
        # if the non-admin actually replies to the prompt, with the real,
        # different text "You are not a group admin!" (no video). Drive that
        # real trigger here and assert the scenario's intent (a non-admin
        # cannot successfully set the welcome message) instead of the copied
        # literal wording, which does not exist anywhere in v1 for this
        # command.
        welcome_ctx.submitted_text = "Attempted welcome text"
        feed(
            run,
            dispatcher,
            bot,
            make_message_update(
                welcome_ctx.submitted_text,
                welcome_ctx.alloc_id(),
                user_id=welcome_ctx.sender_id,
                reply_to=_reply_to_prompt(welcome_ctx),
            ),
        )
        sent = telegram.calls_to("sendMessage")
        assert sent, "expected a sendMessage call, got none"
        assert sent[-1].get("text", "") == welcome_handler.NOT_ADMIN_TEXT, sent[-1]
        return

    sent = telegram.calls_to("sendMessage")
    assert sent, "expected a sendMessage call, got none"
    assert sent[-1].get("text", "") == text, sent[-1]


@then("display a video displaying how to remove anonymous mode from the user settings")
def bot_displays_video(telegram: MockTelegram) -> None:
    """Not reproduced for /newwelcome (see the step above and
    docs/contracts/core_welcome.md) — v1 never sends this video for this
    command. Asserting its absence keeps this step from silently doing
    nothing.
    """
    assert not telegram.calls_to("sendVideo")
    assert not telegram.calls_to("sendAnimation")


@then(
    "the bot should send a message to the group welcoming the new member using the set welcome message."
)
def bot_welcomes_new_member(telegram: MockTelegram) -> None:
    sent = telegram.calls_to("sendMessage")
    assert sent, "expected a sendMessage call, got none"
    # No custom welcome was configured in this scenario (copied verbatim from
    # Cookiebot-QA, no seeding step) - "the set welcome message" is the
    # group's default text.
    assert sent[-1].get("text", "") == locales.get("welcome_user", "en", user="QA Group"), sent[-1]


@then("the bot should send the default welcome message for the group's language")
def bot_sends_default_welcome(telegram: MockTelegram) -> None:
    sent = telegram.calls_to("sendMessage")
    assert sent, "expected a sendMessage call, got none"
    assert sent[-1].get("text", "") == locales.get("welcome_user", "en", user="QA Group"), sent[-1]


@then("the placeholder is replaced with the new member's first name")
def placeholder_replaced_with_first_name(telegram: MockTelegram) -> None:
    sent = telegram.calls_to("sendMessage")
    assert sent, "expected a sendMessage call, got none"
    assert sent[-1].get("text", "") == "Welcome Alex to the crew!", sent[-1]


@then("the welcome message shows the $user/$username collision defect")
def welcome_shows_dollar_username_collision(telegram: MockTelegram) -> None:
    """`$user` is checked before `$username` in `welcome.py`'s `_USER_TAGS` and
    is a literal, undelimited substring of it (docs/contracts/core_welcome.md,
    "A second verified placeholder defect"). The newcomer in this scenario
    ("Alex") has no Telegram username, so the substitution value is the first
    name "Alex": in "hi $username!", the shorter tag "$user" is found and
    replaced first, leaving the "name" tail of "$username" glued onto the
    result -> "hi Alexname!", not the naively-expected "hi Alex!". This is v1's
    real, preserved behaviour, not a v2 regression.
    """
    sent = telegram.calls_to("sendMessage")
    assert sent, "expected a sendMessage call, got none"
    assert sent[-1].get("text", "") == "hi Alexname!", sent[-1]


@then("the welcome message is not updated")
def welcome_not_updated(run: Callable[[Coroutine[Any, Any, Any]], Any]) -> None:
    assert run(welcome_handler._fetch_welcome_body(GROUP_ID)) is None  # noqa: SLF001


@then(
    "the bot should save the new welcome message and display a message confirming that "
    "the welcome message has been updated"
)
def bot_confirms_welcome_updated(
    welcome_ctx: Ctx,
    telegram: MockTelegram,
    run: Callable[[Coroutine[Any, Any, Any]], Any],
) -> None:
    sent = telegram.calls_to("sendMessage")
    assert sent, "expected a sendMessage call, got none"
    assert sent[-1].get("text", "") == welcome_handler.WELCOME_UPDATED_TEXT, sent[-1]
    stored_body = run(welcome_handler._fetch_welcome_body(GROUP_ID))  # noqa: SLF001
    assert stored_body == welcome_ctx.submitted_text


@then("the bot should send a message noting a new bot companion was added")
def bot_notes_new_bot_companion(telegram: MockTelegram) -> None:
    sent = telegram.calls_to("sendMessage")
    assert sent, "expected a sendMessage call, got none"
    assert sent[-1].get("text", "") == locales.get("new_bot_participant", "en"), sent[-1]


@then("no welcome message is sent")
def no_welcome_message_is_sent(telegram: MockTelegram) -> None:
    texts = [c.get("text", "") for c in telegram.calls_to("sendMessage")]
    assert locales.get("welcome_user", "en", user="QA Group") not in texts
    assert locales.get("welcome", "en") not in texts


@then("no welcome message is sent to the group")
def no_welcome_message_sent_to_group(telegram: MockTelegram) -> None:
    assert not telegram.calls_to("sendMessage"), telegram.calls


@then("only the first new member receives the welcome message")
def only_first_joiner_welcomed(telegram: MockTelegram) -> None:
    sent = telegram.calls_to("sendMessage")
    assert len(sent) == 1, sent
    assert sent[0].get("text", "") == "Welcome @first!", sent[0]
