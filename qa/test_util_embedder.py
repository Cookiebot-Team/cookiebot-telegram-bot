"""Step definitions for util_embedder.

QA: qa/features/util_embedder.feature (copied from
../Cookiebot-QA/features/util_embedder.feature, plus scenarios for v1
behaviour the spec never covers — see that file's comment and
docs/contracts/util_embedder.md Phase 3).

Drives the real dispatcher (`cb_gateway.main.dp`, via `qa/conftest.py`'s
session-scoped `dispatcher` fixture) against the mock Telegram API, same as
every other acceptance test in this suite.

`packages/cb-gateway/src/cb_gateway/handlers/__init__.py:build_router()` does
not register `embedder.router` yet (out of this port's file ownership — see
the task's file list). These scenarios will not pass end to end until whoever
owns that file adds `root.include_router(embedder.router)`.

This feature itself needs no database and no Valkey - it reads only
`ctx.config` (which degrades to the v1 defaults, `qa/conftest.py`'s module
docstring) and writes nothing. A real Postgres is still requested below
(`_database`), because the shared dispatcher also runs `core_groupguardian`'s
`_is_captcha_reply` filter over every plain, non-command text message when
`captcha_timeout_seconds > 0` (the v1-matching default) - it does its own
`get_config` + pending-row lookup regardless of which feature the message is
actually for. Without a live pool that filter raises for every scenario here,
since every one of them sends ordinary group text. Skips cleanly (AGENTS.md
§6) when no database is reachable, exactly like every other suite.
"""

from __future__ import annotations

import dataclasses
import time
from typing import TYPE_CHECKING, Any

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from cb_core import group_config
from qa.conftest import GROUP_ID, USER_ID, feed, make_message_update, next_update_id

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from aiogram import Bot, Dispatcher

    from qa.conftest import Context
    from qa.mock_telegram import MockTelegram

scenarios("util_embedder.feature")


@pytest.fixture(autouse=True)
def _database(database: object) -> None:
    """See the module docstring - this feature owns no table of its own."""


BLUESKY_LINK = "https://bsky.app/profile/alice.bsky.social/post/3jt6vw"
BLUESKY_TARGET = "https://fxbsky.app/profile/alice.bsky.social/post/3jt6vw"
TWITTER_LINK = "https://x.com/someuser/status/1234567890123"
TWITTER_TARGET = "https://fixupx.com/someuser/status/1234567890123"
TIKTOK_TARGET = "https://vm.vxtiktok.com/@someuser/video/7123456789012345678"
INVALID_LINK = "https://example.com/cool-article"
ALREADY_EMBEDDED_LINK = "https://fixupx.com/someuser/status/1234567890123"


def _send_text(
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
    text: str,
    *,
    user_id: int = USER_ID,
) -> None:
    feed(run, dispatcher, bot, make_message_update(text, next_update_id(), user_id=user_id))


# --------------------------------------------------------------------- given


@given("that the bot is running and responsive")
def bot_running(ctx: Context) -> None:
    ctx.bot_running = True


@given("the group has utility functions disabled")
def utility_disabled() -> None:
    """v1's `functionsUtility` off (`Configurations.py:111` default is on) -
    `check_reply_embed` is only called `if utilityfunctions:`
    (`COOKIEBOT.py:311`), so a disabled group sees no rewrite at all, silently.

    Seeded directly into `group_config`'s L1, same seam
    `qa/conftest.py`'s `_clean` fixture already clears before every test.
    """
    config = dataclasses.replace(group_config.DEFAULTS, group_id=GROUP_ID, functions_utility=False)
    group_config._l1[GROUP_ID] = (config, time.monotonic() + 9999)  # noqa: SLF001


# ---------------------------------------------------------------------- when


@when(parsers.parse('the user sends a link from "{link}"'))
def sends_a_link(
    run: Callable[[Coroutine[Any, Any, Any]], Any], dispatcher: Dispatcher, bot: Bot, link: str
) -> None:
    """The data-driven Scenario Outlines' own When step: the link itself is
    the table's data column, unlike the fixed-link steps below it (kept as-is
    for the scenarios ported verbatim from Cookiebot-QA and the ones testing a
    link's *position* in the message, not its host)."""
    _send_text(run, dispatcher, bot, link)


@when("the user sends a video link from bluesky")
def sends_bluesky_link(
    run: Callable[[Coroutine[Any, Any, Any]], Any], dispatcher: Dispatcher, bot: Bot
) -> None:
    _send_text(run, dispatcher, bot, BLUESKY_LINK)


@when("the user sends an invalid link")
def sends_invalid_link(
    run: Callable[[Coroutine[Any, Any, Any]], Any], dispatcher: Dispatcher, bot: Bot
) -> None:
    _send_text(run, dispatcher, bot, INVALID_LINK)


@when("the user sends a message with links from both twitter and bluesky")
def sends_multiple_links(
    run: Callable[[Coroutine[Any, Any, Any]], Any], dispatcher: Dispatcher, bot: Bot
) -> None:
    _send_text(run, dispatcher, bot, f"look at these: {TWITTER_LINK} and {BLUESKY_LINK}")


@when("the user sends a link that is already an embedded form")
def sends_already_embedded_link(
    run: Callable[[Coroutine[Any, Any, Any]], Any], dispatcher: Dispatcher, bot: Bot
) -> None:
    _send_text(run, dispatcher, bot, ALREADY_EMBEDDED_LINK)


@when("the user sends a bluesky link surrounded by other words")
def sends_link_in_sentence(
    run: Callable[[Coroutine[Any, Any, Any]], Any], dispatcher: Dispatcher, bot: Bot
) -> None:
    _send_text(run, dispatcher, bot, f"check this out {BLUESKY_LINK} it's great")


@when("the user sends a command containing a video link from bluesky")
def sends_command_with_link(
    run: Callable[[Coroutine[Any, Any, Any]], Any], dispatcher: Dispatcher, bot: Bot
) -> None:
    _send_text(run, dispatcher, bot, f"/somecommand {BLUESKY_LINK}")


# ---------------------------------------------------------------------- then


@then("the bot should reply to the link with an embedded version of it")
def bot_replies_with_embed(telegram: MockTelegram) -> None:
    sent = telegram.calls_to("sendMessage")
    assert sent, "expected a sendMessage call, got none"
    text = sent[-1].get("text", "")
    assert text in {BLUESKY_TARGET, TWITTER_TARGET, TIKTOK_TARGET}, sent[-1]


@then(parsers.parse('the bot should reply with "{target}"'))
def bot_replies_with_exact_target(telegram: MockTelegram, target: str) -> None:
    """The host->target Scenario Outline's own assertion: unlike
    `bot_replies_with_embed` above (set membership, shared by the scenarios
    ported verbatim from Cookiebot-QA), this checks the *specific* rewrite the
    table row asked for -- the whole point of a per-host table."""
    sent = telegram.calls_to("sendMessage")
    assert sent, "expected a sendMessage call, got none"
    assert sent[-1].get("text", "") == target, sent[-1]


@then("the bot should reply with an embedded version of each link")
def bot_replies_with_all_embeds(telegram: MockTelegram) -> None:
    sent = telegram.calls_to("sendMessage")
    assert sent, "expected a sendMessage call, got none"
    lines = sent[-1].get("text", "").split("\n")
    assert lines == [TWITTER_TARGET, BLUESKY_TARGET], sent[-1]


@then("the bot should not respond")
def bot_does_not_respond(telegram: MockTelegram) -> None:
    assert not telegram.calls_to("sendMessage"), telegram.calls
