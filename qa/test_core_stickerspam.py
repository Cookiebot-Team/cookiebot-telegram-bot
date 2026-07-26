"""Step definitions for core_stickerspam.

QA: qa/features/core_stickerspam.feature (copied from
../Cookiebot-QA/features/core_stickerspam.feature, plus scenarios for v1
behaviour the spec never covers — see that file's comment and
docs/contracts/core_stickerspam.md Phase 3).

Drives the real dispatcher (`cb_gateway.main.dp`, via `qa/conftest.py`'s
session-scoped `dispatcher` fixture) against the mock Telegram API, same as
every other acceptance test in this suite.

`packages/cb-gateway/src/cb_gateway/handlers/__init__.py:build_router()` does
not register `stickerspam.router` yet (out of this port's file ownership — see
the task's file list). These scenarios will not pass end to end until whoever
owns that file adds `root.include_router(stickerspam.router)`.

These scenarios run against a real Valkey (the `clean_cache` fixture in
qa/conftest.py, database index 15, flushed around each scenario) and skip when
none is reachable. A dict stand-in for `incr_window` would prove nothing about
the property that matters — that INCR and EXPIRE are one atomic pipeline shared
by every replica, which is the whole reason v1's per-process dict was wrong.
`test_cache_outage_fails_open` closes the cache instead, to prove the real
failure mode.
"""

from __future__ import annotations

import dataclasses
import os
import time
from typing import TYPE_CHECKING, Any

from pytest_bdd import given, parsers, scenarios, then, when

from cb_core import group_config
from qa.conftest import ADMIN_ID, GROUP_ID, USER_ID, feed, make_message_update, next_update_id

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from types import ModuleType

    from aiogram import Bot, Dispatcher

    from qa.conftest import Context
    from qa.mock_telegram import MockTelegram

scenarios("core_stickerspam.feature")

# v1's real default (`Configurations.py:111`), also `GroupConfig.sticker_spam_limit`'s
# dataclass default (`cb_core/group_config.py:53`) — read off the real default
# object rather than retyped, so this suite breaks loudly if the two ever drift.
DEFAULT_LIMIT = group_config.DEFAULTS.sticker_spam_limit


def _send_stickers(
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
    *,
    user_id: int,
    count: int,
) -> None:
    for i in range(count):
        feed(
            run,
            dispatcher,
            bot,
            make_message_update(None, next_update_id(), user_id=user_id, sticker=f"pack-{i}"),
        )


# --------------------------------------------------------------------- given


@given("that the bot is in the group and properly set up")
def bot_set_up(ctx: Context) -> None:
    ctx.bot_running = True


@given("that a user sends more than the set amount of stickers within a period of time")
def user_sends_excessive_stickers(
    clean_cache: None,
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
) -> None:
    _send_stickers(run, dispatcher, bot, user_id=USER_ID, count=DEFAULT_LIMIT + 1)


@given("that the bot is configured to allow sticker spam")
def sticker_spam_allowed() -> None:
    """v1 has no on/off switch for this feature — `stickerSpamLimit` is only
    ever a number (`Configurations.py:111`, backend `Config.java:23`). The only
    real lever an admin has is a limit high enough that no realistic flood
    trips it, so that is what "configured to allow sticker spam" means here.
    Seeded directly into `group_config`'s L1 (the same seam
    `qa/conftest.py`'s `_clean` fixture already clears before every test),
    since this suite has no database for `group_configs` to seed a real row
    into.
    """
    config = dataclasses.replace(
        group_config.DEFAULTS, group_id=GROUP_ID, sticker_spam_limit=1_000_000
    )
    group_config._l1[GROUP_ID] = (config, time.monotonic() + 9999)  # noqa: SLF001


@given("that a user sends stickers just under the set amount within a period of time")
def user_sends_almost_the_limit(
    clean_cache: None,
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
) -> None:
    _send_stickers(run, dispatcher, bot, user_id=USER_ID, count=DEFAULT_LIMIT - 1)


@given(parsers.parse("a user sends stickers so that the total is {offset:d} relative to the limit"))
def user_sends_stickers_relative_to_limit(
    clean_cache: None,
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
    offset: int,
) -> None:
    _send_stickers(run, dispatcher, bot, user_id=USER_ID, count=DEFAULT_LIMIT + offset)


@given("that an admin sends more than the set amount of stickers within a period of time")
def admin_sends_excessive_stickers(
    clean_cache: None,
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
    telegram: MockTelegram,
) -> None:
    # ADMIN_ID is already an administrator by default (qa/conftest.py's `_clean`
    # fixture) — no extra setup needed to prove there is no admin exemption.
    assert any(a["user"]["id"] == ADMIN_ID for a in telegram.admins.get(GROUP_ID, []))
    _send_stickers(run, dispatcher, bot, user_id=ADMIN_ID, count=DEFAULT_LIMIT + 1)


# ---------------------------------------------------------------------- when


@when("the bot detects the sticker spam")
def bot_detects_sticker_spam() -> None:
    """The Given step above already fed every sticker; nothing further to do."""


@when("a user sends more than the set amount of stickers within a period of time")
def user_sends_more_than_configured_amount(
    clean_cache: None,
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
) -> None:
    _send_stickers(run, dispatcher, bot, user_id=USER_ID, count=DEFAULT_LIMIT + 1)


@when("the user sends yet another sticker")
def user_sends_one_more_sticker(
    clean_cache: None,
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
) -> None:
    _send_stickers(run, dispatcher, bot, user_id=USER_ID, count=1)


@when(
    "a different user in the same group sends a sticker that pushes the group past the set amount"
)
def different_user_pushes_group_past_limit(
    clean_cache: None,
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
) -> None:
    assert USER_ID + 999 != USER_ID
    _send_stickers(run, dispatcher, bot, user_id=USER_ID + 999, count=1)


# ---------------------------------------------------------------------- then


@then("the bot should issue a warning to the user about excessive sticker usage")
def bot_warns_about_stickers(telegram: MockTelegram) -> None:
    from cb_core import locales

    sent = telegram.calls_to("sendMessage")
    assert sent, "expected a sendMessage call, got none"
    assert sent[-1].get("text", "") == locales.get("flood_stickers", "en"), sent[-1]


@then("the bot should not issue any warnings")
def bot_issues_no_warnings(telegram: MockTelegram) -> None:
    assert not telegram.calls_to("sendMessage"), telegram.calls


@then("the bot should delete the sticker message")
def bot_deletes_sticker(telegram: MockTelegram) -> None:
    assert telegram.calls_to("deleteMessage"), "expected a deleteMessage call, got none"


@then(parsers.parse('the bot\'s resulting action is "{action}"'))
def bot_resulting_action_is(telegram: MockTelegram, action: str) -> None:
    """Boundary check for the warn-at-`==`-limit, delete-at-`>`-limit rule
    (docs/contracts/core_stickerspam.md "Counting logic"). One fewer than the
    limit produces neither call. Exactly the limit produces the warning, and
    nothing has crossed the limit yet so there is no deletion. One more than
    the limit has, by construction (the batch also sends the limit-th sticker
    on the way there), already produced both the warning and a deletion — this
    row only asserts the deletion, the marginal action it exists to prove.
    """
    from cb_core import locales

    sent = telegram.calls_to("sendMessage")
    deleted = telegram.calls_to("deleteMessage")
    if action == "nothing":
        assert not sent, sent
        assert not deleted, deleted
    elif action == "warning":
        assert sent, "expected a sendMessage call, got none"
        assert sent[-1].get("text", "") == locales.get("flood_stickers", "en"), sent[-1]
        assert not deleted, deleted
    elif action == "deletion":
        assert deleted, "expected a deleteMessage call, got none"
    else:
        raise AssertionError(f"unrecognized action {action!r}")


# ------------------------------------------------------------- not from Gherkin


def test_cache_outage_fails_open_not_closed(
    valkey: ModuleType,
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
    telegram: MockTelegram,
) -> None:
    """A real outage, not a simulated one: the client is closed for the duration.

    `cb_core.cache.client()` then raises exactly what it raises in production
    when Valkey is unreachable, so this exercises the real path the fail-open
    decision has to survive (docs/contracts/core_stickerspam.md). A flood of
    stickers with the cache down must produce neither a warning nor a deletion:
    silence, not "everything is spam".
    """
    from cb_core.settings import Settings

    run(valkey.close_cache())
    try:
        for i in range(DEFAULT_LIMIT + 5):
            feed(
                run,
                dispatcher,
                bot,
                make_message_update(None, next_update_id(), user_id=USER_ID, sticker=f"outage-{i}"),
            )
        assert not telegram.calls_to("sendMessage"), "a down cache must not produce a spam warning"
        assert not telegram.calls_to("deleteMessage"), (
            "a down cache must not delete a legitimate sticker"
        )
    finally:
        # The fixture is session-scoped; put it back for whatever runs next.
        dsn = os.environ.get("CB_TEST_REDIS_DSN", "redis://localhost:6379/15")
        run(valkey.init_cache(Settings(redis_dsn=dsn, service_name="cb-qa", traces_enabled=False)))
