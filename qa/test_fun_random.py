"""Step definitions for fun_random.

QA: qa/features/fun_random.feature (synced from Cookiebot-QA/features/fun_random.feature).
Contract: docs/contracts/fun_random.md.

Drives the real dispatcher (`cb_gateway.main.dp`, via `qa/conftest.py`'s
`dispatcher` fixture) against the mock Telegram API and a real `media_objects`
table — `cb_gateway.handlers.fun_random` reads and writes that table through
`cb_core.storage.media()`, and AGENTS.md §6 forbids mocking our own code in an
acceptance test, so the pool a scenario "has access to" is a real row, seeded
directly through `storage.media().put(...)` the same way
`qa/test_core_rules.py`'s `rules_are_configured` seeds `group_rules` ahead of
the behaviour actually under test. The blob store behind it is `memory://`
(`cb_core.settings.Settings.storage_uri`'s own default) — a real
`MediaService`, just without a cloud account, matching
`qa/integration/test_media_service.py`'s own fixture.

Ingestion (pooling a freshly-posted photo/video) is deliberately not exercised
here: it requires downloading file bytes through `Bot.download`, which calls
Telegram's `getFile` — a method `qa/mock_telegram.py` does not implement (out
of this feature's file ownership). That path is covered instead by
`packages/cb-gateway/tests/test_fun_random.py` (the pure predicates, with a
fake bot) and `qa/integration/test_fun_random.py` (the real database write).

NOTE: `packages/cb-gateway/src/cb_gateway/handlers/__init__.py` does not
register `fun_random.router` yet (out of this feature's file ownership — see
the task's file list; several sibling ports, e.g. `core_rules`,
`core_mediarestrict`, note the exact same gap). These scenarios stay red until
whoever owns that file adds `root.include_router(fun_random.router)`.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Iterator
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from pytest_bdd import given, parsers, scenarios, then, when

from cb_core import group_config, locales
from cb_core.settings import Settings
from qa.conftest import GROUP_ID, Context, feed, make_message_update, next_update_id
from qa.mock_telegram import MockTelegram

scenarios("fun_random.feature")

SAFE_FILE_ID = "qa-random-safe-photo"
UNSAFE_FILE_ID = "qa-random-unsafe-photo"


# ------------------------------------------------------------------ fixtures


@pytest.fixture(scope="module", autouse=True)
def _media_storage(run: Callable[[Coroutine[Any, Any, Any]], Any]) -> Iterator[None]:
    """A real `MediaService` over an in-memory blob store.

    Defensive about a session shared with other test modules: only this
    fixture's own `init_storage`/`close_storage` pair runs if nothing has
    initialised storage yet, so a module ordering that already brought it up
    for another feature is left alone.
    """
    from cb_core import storage

    already_initialised = True
    try:
        storage.media()
    except RuntimeError:
        already_initialised = False

    if not already_initialised:
        run(storage.init_storage(Settings(service_name="cb-qa-fun-random", traces_enabled=False)))
    yield
    if not already_initialised:
        run(storage.close_storage())


@pytest.fixture(autouse=True)
def _clean_media(database: Any, run: Callable[[Coroutine[Any, Any, Any]], Any]) -> Iterator[None]:
    """The real `media_objects` table, truncated for this group around each
    scenario. Requires the real database (`database` fixture skips cleanly
    when unreachable, same as every other DB-backed acceptance suite)."""
    from cb_core import db

    stmt = "DELETE FROM media_objects WHERE group_id = $1"
    run(db.execute(stmt, GROUP_ID, name="qa_clean_fun_random_media"))
    yield
    run(db.execute(stmt, GROUP_ID, name="qa_clean_fun_random_media"))


@pytest.fixture(autouse=True)
def _reset_config(run: Callable[[Coroutine[Any, Any, Any]], Any]) -> Iterator[None]:
    """`group_config._l1` is process-global; scenarios below flip
    `functions_fun`/`sfw` and must not leak into a later scenario reusing the
    same `GROUP_ID` (same fix `qa/test_core_mediarestrict.py` applies)."""
    yield
    run(group_config.set_config(GROUP_ID, functions_fun=True, sfw=True))
    group_config._l1.clear()  # noqa: SLF001


class Ctx:
    """Per-scenario state on top of qa/conftest.py's base `Context`."""

    def alloc_id(self) -> int:
        # Shared process-wide counter: a per-scenario counter collides with
        # earlier scenarios and the dedupe middleware drops the update.
        return next_update_id()


@pytest.fixture
def fr_ctx() -> Ctx:
    return Ctx()


def _seed(run: Callable[[Coroutine[Any, Any, Any]], Any], *, file_id: str, sfw: bool) -> None:
    from cb_core import storage

    run(
        storage.media().put(
            GROUP_ID,
            "photo",
            f"random test bytes for {file_id}".encode(),
            content_type="image/jpeg",
            telegram_file_id=file_id,
            sfw=sfw,
        )
    )


# --------------------------------------------------------------------- given


@given("that the bot is in the group and properly set up")
def bot_set_up(ctx: Context) -> None:
    ctx.bot_running = True


@given("that the bot has access to media from groups it is in")
def pool_has_media(run: Callable[[Coroutine[Any, Any, Any]], Any]) -> None:
    _seed(run, file_id=SAFE_FILE_ID, sfw=True)


@given("that the bot has no media collected for this group")
def pool_is_empty() -> None:
    """No-op precondition: `_clean_media` above already truncates
    `media_objects` for this group before every scenario runs."""


@given("that fun functions are disabled for the group")
def fun_disabled(run: Callable[[Coroutine[Any, Any, Any]], Any]) -> None:
    run(group_config.set_config(GROUP_ID, functions_fun=False))


@given("that the bot has access to both safe and unsafe media from groups it is in")
def pool_has_safe_and_unsafe_media(run: Callable[[Coroutine[Any, Any, Any]], Any]) -> None:
    _seed(run, file_id=SAFE_FILE_ID, sfw=True)
    _seed(run, file_id=UNSAFE_FILE_ID, sfw=False)


@given("that the bot has access to unsafe media from groups it is in")
def pool_has_only_unsafe_media(run: Callable[[Coroutine[Any, Any, Any]], Any]) -> None:
    _seed(run, file_id=UNSAFE_FILE_ID, sfw=False)


@given("the group is configured as safe-for-work")
def group_is_sfw(run: Callable[[Coroutine[Any, Any, Any]], Any]) -> None:
    run(group_config.set_config(GROUP_ID, sfw=True))


@given("the group is not configured as safe-for-work")
def group_is_not_sfw(run: Callable[[Coroutine[Any, Any, Any]], Any]) -> None:
    run(group_config.set_config(GROUP_ID, sfw=False))


# ---------------------------------------------------------------------- when


@when(parsers.parse('the user sends the command "{command}"'))
def user_sends_command(
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
    fr_ctx: Ctx,
    command: str,
) -> None:
    feed(run, dispatcher, bot, make_message_update(command, fr_ctx.alloc_id()))


# ---------------------------------------------------------------------- then


@then("the bot should respond with a random media from one of the groups it is in")
def bot_sends_media(telegram: MockTelegram) -> None:
    sent = telegram.calls_to("sendPhoto") + telegram.calls_to("sendVideo")
    assert sent, "expected a sendPhoto or sendVideo call, got none"
    assert int(sent[-1].get("chat_id", 0)) == GROUP_ID


@then("the media should be appropriate for the group it is sent in")
def media_is_appropriate(telegram: MockTelegram) -> None:
    """The only media ever seeded in this scenario's Background is the one
    marked `sfw=True`, so "appropriate for the group" is: exactly that item."""
    sent = telegram.calls_to("sendPhoto")
    assert sent, "expected a sendPhoto call"
    assert sent[-1].get("photo") == SAFE_FILE_ID


@then("the media sent is never the unsafe one")
def media_is_never_unsafe(telegram: MockTelegram) -> None:
    sent = telegram.calls_to("sendPhoto")
    assert sent, "expected a sendPhoto call"
    assert sent[-1].get("photo") == SAFE_FILE_ID


@then("the media sent may be either the safe or unsafe one")
def media_may_be_either(telegram: MockTelegram) -> None:
    """The sfw-off row of the sfw Scenario Outline: with the gate open, both
    seeded items are eligible, so the only honest assertion is membership, not
    equality to either one -- asserting a specific pick would be flaky, not
    stricter."""
    sent = telegram.calls_to("sendPhoto")
    assert sent, "expected a sendPhoto call"
    assert sent[-1].get("photo") in {SAFE_FILE_ID, UNSAFE_FILE_ID}


@then("the bot should display a message saying fun functions are disabled")
def bot_says_fun_off(telegram: MockTelegram) -> None:
    sent = telegram.calls_to("sendMessage")
    assert sent, "expected a sendMessage call, got none"
    assert sent[-1].get("text", "") == locales.get("fun_off", "en")


@then("the user receives no response")
def no_response(telegram: MockTelegram) -> None:
    assert not telegram.calls_to("sendMessage")
    assert not telegram.calls_to("sendPhoto")
    assert not telegram.calls_to("sendVideo")
    assert not telegram.calls_to("sendAnimation")
