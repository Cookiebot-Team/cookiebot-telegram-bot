"""Step definitions for core_rules.

QA: qa/features/core_rules.feature (synced from Cookiebot-QA/features/core_rules.feature).
Contract: docs/contracts/core_rules.md.

Drives the real dispatcher (`cb_gateway.main.dp`, via `qa/conftest.py`'s
`dispatcher` fixture) against the mock Telegram API, same as every other
acceptance test in this suite. The one thing faked here is the `group_rules`
table itself: `cb_gateway.handlers.rules._fetch_rules` / `_upsert_rules` are the
DB seam this handler owns, and this suite runs offline (no Postgres — see
`qa/conftest.py`'s module docstring), so they are monkeypatched to an
in-process dict for the duration of each scenario. Everything else — parsing,
filters, `context_for`, admin resolution against the mock's
`getChatAdministrators`, the actual Telegram calls — is real. The real
Postgres round trip for `group_rules` is covered separately by
`qa/integration/test_group_rules.py`.

NOTE: `packages/cb-gateway/src/cb_gateway/handlers/__init__.py` does not
register `rules.router` yet (out of this feature's file ownership — see the
task's file list). These scenarios will not pass end to end until whoever owns
that file adds `root.include_router(rules.router)`.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Iterator
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from pytest_bdd import given, parsers, scenarios, then, when

from cb_core import admins as admins_module
from cb_gateway.handlers import rules as rules_handler
from qa.conftest import (
    ADMIN_ID,
    GROUP_ID,
    USER_ID,
    Context,
    feed,
    make_message_update,
    next_update_id,
)
from qa.mock_telegram import MockTelegram

scenarios("core_rules.feature")

SEEDED_RULES_TEXT = "Be nice to each other. Have fun!"


@pytest.fixture(autouse=True)
def _real_rules_table(clean_rules: None) -> None:
    """The real `group_rules` table, truncated for this group around each scenario.

    Not a monkeypatched store: `/rules` answers *with* what was persisted, so
    faking the persistence would leave the scenario proving only that the handler
    can echo a dict back to itself. AGENTS.md §6 allows an acceptance scenario to
    need a database and forbids mocking our own code in one; `clean_rules` skips
    the suite when no database is reachable.
    """


@pytest.fixture(autouse=True)
def _fresh_admin_cache() -> Iterator[None]:
    """`cb_core.admins._l1` is a process-global, TTL'd dict, and every scenario
    in this file reuses the same `GROUP_ID` (`qa/conftest.py`). Without a clear
    here, one scenario's `telegram.set_admins(...)` result stays cached and
    leaks into the next scenario for up to `config_cache_l1_seconds` (30s by
    default) — same fix `qa/integration/test_group_config.py` already applies
    to `group_config._l1` for the same reason, scoped to this test module
    rather than touching the shared `qa/conftest.py`.
    """
    admins_module._l1.clear()  # noqa: SLF001
    yield
    admins_module._l1.clear()  # noqa: SLF001


class Ctx:
    """Extra per-scenario state on top of qa/conftest.py's base `Context`."""

    def __init__(self) -> None:
        self.sender_id = USER_ID
        self.new_rules_prompt: dict[str, Any] | None = None
        self.submitted_text: str | None = None

    def alloc_id(self) -> int:
        # Shared process-wide counter: a per-scenario counter collides with
        # earlier scenarios and the dedupe middleware drops the update.
        return next_update_id()


@pytest.fixture
def rules_ctx() -> Ctx:
    return Ctx()


def _reply_to_prompt(rules_ctx: Ctx) -> dict[str, Any]:
    assert rules_ctx.new_rules_prompt is not None, "no /newrules prompt was sent yet"
    return rules_ctx.new_rules_prompt


# --------------------------------------------------------------------- given


@given("that the bot is in the group and properly set up")
def bot_set_up(ctx: Context) -> None:
    ctx.bot_running = True


@given("the group already has rules configured")
def rules_are_configured(run: Callable[[Coroutine[Any, Any, Any]], Any]) -> None:
    run(rules_handler._upsert_rules(GROUP_ID, ADMIN_ID, SEEDED_RULES_TEXT))  # noqa: SLF001


@given(parsers.parse("the user sends the command {command}"))
def user_sends_command(
    ctx: Context,
    rules_ctx: Ctx,
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
    telegram: MockTelegram,
    command: str,
) -> None:
    update_id = rules_ctx.alloc_id()
    feed(run, dispatcher, bot, make_message_update(command, update_id, user_id=rules_ctx.sender_id))
    if command.split("@")[0] in ("/newrules", "/novasregras", "/nuevasreglas"):
        # The mock records the raw request payload, not the response Telegram
        # would hand back, so the prompt's own message id is fabricated — the
        # handler only ever compares `reply_to_message.text`, never its id.
        #
        # Guarded (not a bare [-1] index): this branch is also reached for a
        # foreign-bot-addressed command, e.g. "/newrules@SomeOtherBot" in the
        # "addressed at a different bot" outline below, where the handler never
        # fires and `sendMessage` was never called — nothing to capture there.
        sent_calls = telegram.calls_to("sendMessage")
        if sent_calls:
            sent = sent_calls[-1]
            rules_ctx.new_rules_prompt = {
                "message_id": update_id,
                "date": sent.get("date", 0) or 0,
                "chat": {"id": GROUP_ID, "type": "supergroup", "title": "QA Group"},
                "from": {
                    "id": 424242,
                    "is_bot": True,
                    "first_name": "Cookiebot",
                    "username": "CookieMWbot",
                },
                "text": sent.get("text", ""),
            }


# ---------------------------------------------------------------------- when


@when("the bot receives the command")
def bot_receives_command() -> None:
    """The Given step above already fed the update; nothing further to do."""


@when("the user is an admin on that group")
def user_is_admin(rules_ctx: Ctx, telegram: MockTelegram) -> None:
    telegram.set_admins(GROUP_ID, [(rules_ctx.sender_id, "administrator")])


@when("the user is not an admin on that group")
def user_is_not_admin(rules_ctx: Ctx, telegram: MockTelegram) -> None:
    telegram.set_admins(GROUP_ID, [(ADMIN_ID, "administrator")])
    assert rules_ctx.sender_id != ADMIN_ID


@when("a user who is not an admin on that group replies to the bot's prompt with new rules text")
def non_admin_replies(
    ctx: Context,
    rules_ctx: Ctx,
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
    telegram: MockTelegram,
) -> None:
    telegram.set_admins(GROUP_ID, [(ADMIN_ID, "administrator")])
    rules_ctx.submitted_text = "No rules, do whatever you want."
    feed(
        run,
        dispatcher,
        bot,
        make_message_update(
            rules_ctx.submitted_text,
            rules_ctx.alloc_id(),
            user_id=USER_ID,
            reply_to=_reply_to_prompt(rules_ctx),
        ),
    )


@when("an anonymous admin on that group replies to the bot's prompt with new rules text")
def anonymous_admin_replies(
    ctx: Context,
    rules_ctx: Ctx,
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
) -> None:
    rules_ctx.submitted_text = "Be kind. No spam."
    feed(
        run,
        dispatcher,
        bot,
        make_message_update(
            rules_ctx.submitted_text,
            rules_ctx.alloc_id(),
            reply_to=_reply_to_prompt(rules_ctx),
            anonymous=True,
        ),
    )


# ---------------------------------------------------------------------- then


@then("the bot should send a message to the group displaying the set rules for that group")
def bot_shows_rules(telegram: MockTelegram) -> None:
    sent = telegram.calls_to("sendMessage")
    assert sent, "expected a sendMessage call, got none"
    body = sent[-1].get("text", "")
    assert SEEDED_RULES_TEXT in body, body
    # Tagline appended (GroupShield.py:60-62) since the seeded text does not
    # end with "@MekhyW".
    assert "Questions about the bot?" in body, body


@then(parsers.parse('the bot should send a message to the group saying "{text}"'))
def bot_says_to_group(telegram: MockTelegram, text: str) -> None:
    """QA's quoted text here paraphrases v1's real `no_rules` catalog string
    rather than quoting it verbatim (docs/contracts/core_rules.md) — v1 code
    wins for the actual observable output, so this asserts against the ported
    catalog value (never retyped here) instead of the spec's prose.
    """
    from cb_core import locales

    sent = telegram.calls_to("sendMessage")
    assert sent, "expected a sendMessage call, got none"
    assert sent[-1].get("text", "") == locales.get("no_rules", "en"), (text, sent[-1])


_CONFIGURAR_TEXT_COPIED_INTO_THIS_SPEC_BY_MISTAKE = (
    "You don't have permission to use this command or are in anonymous mode"
)


@then(parsers.parse('the bot should send a message on the group saying "{text}"'))
def bot_says_on_group(telegram: MockTelegram, text: str) -> None:
    sent = telegram.calls_to("sendMessage")
    assert sent, "expected a sendMessage call, got none"
    body = sent[-1].get("text", "")
    if text == _CONFIGURAR_TEXT_COPIED_INTO_THIS_SPEC_BY_MISTAKE:
        # This QA scenario's quoted text is v1's /configurar rejection, not
        # /newrules's (docs/contracts/core_rules.md, mismatch #1) — v1 never
        # gates /newrules on admin status at all, it always answers with the
        # same prompt. Assert the real, observable v1 behaviour instead of
        # retyping either string.
        assert body == rules_handler.NEW_RULES_PROMPT, body
        return
    assert body == text, body


@then(parsers.parse('the bot should display the message "{text}"'))
def bot_displays_message(telegram: MockTelegram, text: str) -> None:
    sent = telegram.calls_to("sendMessage")
    assert sent, "expected a sendMessage call, got none"
    assert sent[-1].get("text", "") == text, sent[-1]


@then("display a video displaying how to remove anonymous mode from the user settings")
def bot_displays_video(telegram: MockTelegram) -> None:
    # v1's /newrules never sends this video — see `bot_says_on_group` above and
    # docs/contracts/core_rules.md mismatch #1. Its absence *is* the real,
    # observable v1 behaviour for this command.
    assert not telegram.calls_to("sendVideo")
    assert not telegram.calls_to("sendAnimation")


@then("the admin should be able to reply to the bot's message with the new rules")
def admin_replies_with_new_rules(
    rules_ctx: Ctx,
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
) -> None:
    rules_ctx.submitted_text = "Be nice, no spam. Have fun!"
    feed(
        run,
        dispatcher,
        bot,
        make_message_update(
            rules_ctx.submitted_text,
            rules_ctx.alloc_id(),
            user_id=rules_ctx.sender_id,
            reply_to=_reply_to_prompt(rules_ctx),
        ),
    )


@then(
    "the bot should save the new rules and display a message confirming that the rules have been updated"
)
def bot_confirms_rules_updated(
    rules_ctx: Ctx, telegram: MockTelegram, run: Callable[[Coroutine[Any, Any, Any]], Any]
) -> None:
    sent = telegram.calls_to("sendMessage")
    assert sent, "expected a sendMessage call, got none"
    assert sent[-1].get("text", "") == "Updated rules message! ✅", sent[-1]
    stored_body = run(rules_handler._fetch_rules(GROUP_ID))  # noqa: SLF001
    assert stored_body == rules_ctx.submitted_text


@then("the user receives no response")
def no_response(telegram: MockTelegram) -> None:
    assert not telegram.calls_to("sendMessage"), telegram.calls
