"""Step definitions for core_mediarestrict.

QA: qa/features/core_mediarestrict.feature (synced from
Cookiebot-QA/features/core_mediarestrict.feature). Contract:
docs/contracts/core_mediarestrict.md.

Drives the real dispatcher (`cb_gateway.main.dp`, via `qa/conftest.py`'s
`dispatcher` fixture) against the mock Telegram API and a real `group_members`
table, same as every other acceptance test in this suite. `group_members` is
not monkeypatched: whether a member is restricted *is* the state persisted
there (AGENTS.md §6 forbids mocking our own code in an acceptance test), so
`clean_members` (real DB, skips cleanly when unreachable) is required rather
than an in-process fake.

Preconditions that establish a member's join time call
`cb_gateway.handlers.mediarestrict._record_join` / raw SQL directly rather than
feeding a join update through the dispatcher — the same idiom
`qa/test_core_rules.py`'s `rules_are_configured` and
`qa/integration/test_group_welcomes.py` use to seed persisted state ahead of
the behaviour actually under test.

NOTE: `packages/cb-gateway/src/cb_gateway/handlers/__init__.py` does not
register `mediarestrict.router` yet (out of this feature's file ownership —
see the task's file list; `welcome.router` is already registered, `rules.router`
and `config_menu.router` are not, so this port is not alone). The "attempts to
post media" scenarios below feed a real message update through the real
dispatcher and will stay red until whoever owns that file adds
`root.include_router(mediarestrict.router)`.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Iterator
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from pytest_bdd import given, parsers, scenarios, then, when

from cb_core import group_config, locales
from cb_gateway.handlers import mediarestrict
from qa.conftest import (
    GROUP_ID,
    NEWCOMER_ID,
    USER_ID,
    Context,
    feed,
    make_message_update,
    next_update_id,
)
from qa.mock_telegram import MockTelegram

scenarios("core_mediarestrict.feature")

# The Scenario Outline's `<content type>` values match `make_message_update`'s
# own media keyword arguments 1:1.
_CONTENT_TYPES = frozenset({"photo", "video", "animation", "sticker"})


@pytest.fixture(autouse=True)
def _real_members_table(clean_members: None) -> None:
    """The real `group_members` table, truncated for this group around each
    scenario. Restriction depends on the persisted `joined_at`, so faking it
    would only prove the handler can echo back a value it just wrote.
    """


@pytest.fixture(autouse=True)
def _reset_config(run: Callable[[Coroutine[Any, Any, Any]], Any]) -> Iterator[None]:
    """`group_config._l1` is process-global; the "disabled" scenario changes
    `media_restrict_seconds` and must not leak into a later scenario reusing
    the same `GROUP_ID` (same fix `test_core_rules.py` applies to the admin
    cache, and `qa/integration/test_group_config.py` applies to this one)."""
    yield
    run(group_config.set_config(GROUP_ID, media_restrict_seconds=600))
    group_config._l1.clear()  # noqa: SLF001


class Ctx:
    """Extra per-scenario state on top of qa/conftest.py's base `Context`."""

    def __init__(self) -> None:
        self.last_message_id: int | None = None

    def alloc_id(self) -> int:
        # Shared process-wide counter: a per-scenario counter collides with
        # earlier scenarios and the dedupe middleware drops the update.
        return next_update_id()


@pytest.fixture
def mr_ctx() -> Ctx:
    return Ctx()


def _post_media(
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
    mr_ctx: Ctx,
    *,
    user_id: int,
    content: str = "photo",
) -> None:
    update_id = mr_ctx.alloc_id()
    mr_ctx.last_message_id = update_id
    kwargs = {"photo": False, "video": False, "animation": False, "sticker": None}
    if content == "sticker":
        kwargs["sticker"] = "pack1"
    else:
        kwargs[content] = True
    feed(
        run,
        dispatcher,
        bot,
        make_message_update(None, update_id, user_id=user_id, **kwargs),
    )


# --------------------------------------------------------------------- given


@given("that the bot is in the group and properly set up")
def bot_set_up(ctx: Context) -> None:
    ctx.bot_running = True


@given("that a new user joins the group")
def new_user_joins(run: Callable[[Coroutine[Any, Any, Any]], Any]) -> None:
    run(mediarestrict._record_join(GROUP_ID, NEWCOMER_ID))  # noqa: SLF001


@given(
    "that an existing user is in the group for more than the time limit set for media restrictions"
)
def existing_user_in_group(run: Callable[[Coroutine[Any, Any, Any]], Any]) -> None:
    from cb_core import db

    run(mediarestrict._record_join(GROUP_ID, USER_ID))  # noqa: SLF001
    # Backdate well past the default 600s window so this precondition holds
    # regardless of how long the test suite takes to reach the assertion.
    run(
        db.execute(
            """
            UPDATE group_members SET joined_at = now() - interval '2 hours'
            WHERE group_id = $1 AND user_id = $2
            """,
            GROUP_ID,
            USER_ID,
            name="qa_backdate_member_join",
        )
    )


@given("that media restriction is disabled for the group")
def media_restriction_disabled(run: Callable[[Coroutine[Any, Any, Any]], Any]) -> None:
    run(group_config.set_config(GROUP_ID, media_restrict_seconds=0))


# ---------------------------------------------------------------------- when


@when("the new user attempts to post media content")
def new_user_posts_media(
    run: Callable[[Coroutine[Any, Any, Any]], Any], dispatcher: Dispatcher, bot: Bot, mr_ctx: Ctx
) -> None:
    _post_media(run, dispatcher, bot, mr_ctx, user_id=NEWCOMER_ID)


@when("the user attempts to post media content")
def existing_user_posts_media(
    run: Callable[[Coroutine[Any, Any, Any]], Any], dispatcher: Dispatcher, bot: Bot, mr_ctx: Ctx
) -> None:
    _post_media(run, dispatcher, bot, mr_ctx, user_id=USER_ID)


@when("an admin attempts to post media content")
def admin_posts_media(
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
    telegram: MockTelegram,
    mr_ctx: Ctx,
) -> None:
    telegram.set_admins(GROUP_ID, [(NEWCOMER_ID, "administrator")])
    _post_media(run, dispatcher, bot, mr_ctx, user_id=NEWCOMER_ID)


@when(parsers.parse("the new user attempts to post a {content_type}"))
def new_user_posts_content_type(
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
    mr_ctx: Ctx,
    content_type: str,
) -> None:
    assert content_type in _CONTENT_TYPES, f"unknown content type {content_type!r}"
    _post_media(run, dispatcher, bot, mr_ctx, user_id=NEWCOMER_ID, content=content_type)


# ---------------------------------------------------------------------- then


@then("the bot should prevent the new user from posting media and display a warning message")
def bot_prevents_and_warns(telegram: MockTelegram, mr_ctx: Ctx) -> None:
    deleted = telegram.calls_to("deleteMessage")
    assert deleted, "expected the restricted media message to be deleted"
    assert int(deleted[-1].get("message_id", 0)) == mr_ctx.last_message_id

    sent = telegram.calls_to("sendMessage")
    assert sent, "expected a warning sendMessage call"
    assert sent[-1].get("text", "") == locales.get("restrict_message", "en", time=10)


@then("the bot should allow the existing user to post media without any restrictions")
def bot_allows_existing_user(telegram: MockTelegram) -> None:
    assert not telegram.calls_to("deleteMessage")
    assert not telegram.calls_to("sendMessage")


@then("the bot should allow the new user to post media without any restrictions")
def bot_allows_new_user(telegram: MockTelegram) -> None:
    assert not telegram.calls_to("deleteMessage")
    assert not telegram.calls_to("sendMessage")


@then("the bot should allow the admin to post media without any restrictions")
def bot_allows_admin(telegram: MockTelegram) -> None:
    assert not telegram.calls_to("deleteMessage")
    assert not telegram.calls_to("sendMessage")


@then("the warning message states the configured restriction time in minutes")
def warning_states_minutes(telegram: MockTelegram) -> None:
    sent = telegram.calls_to("sendMessage")
    assert sent, "expected a warning sendMessage call"
    # media_restrict_seconds defaults to 600 -> round(600/60) == 10 minutes,
    # GroupShield.py:149.
    assert sent[-1].get("text", "") == locales.get("restrict_message", "en", time=10)
    assert "10" in sent[-1]["text"]
