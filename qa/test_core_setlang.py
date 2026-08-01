"""Step definitions for core_setlang.

QA: qa/features/core_setlang.feature (synced from ../Cookiebot-QA/features/core_setlang.feature).
Contract: docs/contracts/core_setlang.md.

The copied QA scenarios describe a **web settings page** with a language picker;
v1 has no such surface (docs/site/content/docs/feature-map.mdx already records the mismatch). The
first three scenarios below are kept verbatim (per AGENTS.md §1, "QA wins for
intent") and their steps drive the one real, in-chat mechanism this port owns —
first-contact language derivation from the adder's Telegram client — since that
is the closest real trigger to "the user picks a language and the bot starts
responding in it." The remaining scenarios were added to cover that mechanism
directly, plus the missing-`language_code` and rejected-`setMyCommands` cases.

`setlang.router` is not wired into `cb_gateway.main.dp` (out of scope for this
port — `handlers/__init__.py` is another feature's file, see the module
docstring in `handlers/setlang.py`), so this module overrides the `dispatcher`
fixture, scoped to this file only, with a standalone `Dispatcher` carrying just
`setlang.router` — the same shape `qa/test_util_config.py` used before
`config_menu.router` was wired in. A real database is required (the
`database` fixture): unlike most of this suite, this feature's whole point is a
row landing in `group_configs`, so faking that would prove nothing.
"""

from __future__ import annotations

import itertools
import json
import random
import time
from collections.abc import Callable, Coroutine, Iterator
from types import ModuleType
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from pytest_bdd import given, parsers, scenarios, then, when

from cb_core import db, locales
from qa.conftest import BOT_USERNAME, feed, next_update_id
from qa.conftest import USER_ID as ADDER_ID
from qa.mock_telegram import MockTelegram

scenarios("core_setlang.feature")

# The mock's own `getMe` id (qa/mock_telegram.py:_result), duplicated here the
# same way qa/conftest.py's make_callback_update hardcodes it rather than
# importing a shared constant.
_BOT_ID = 424242

_LANGUAGE_NAME_TO_TELEGRAM_CODE = {
    "Spanish": "es-ES",
    "English": "en-US",
    "Brazilian Portuguese": "pt-BR",
}
_LANGUAGE_NAME_TO_CANONICAL = {
    "Spanish": "es",
    "English": "en",
    "Brazilian Portuguese": "pt",
}

_GROUP_BASE = -1_00_000_000_000 - random.randrange(1, 9_000_000)
_group_seq = itertools.count(1)


def _next_group_id() -> int:
    return _GROUP_BASE - next(_group_seq) * 1000


async def _create_bare_group(group_id: int) -> None:
    """A `groups` row with deliberately **no** `group_configs` row — the FK
    `group_config.set_config` needs, and the "no stored language yet" state
    the scenarios start from."""
    await db.execute(
        """
        INSERT INTO groups (group_id, title, chat_type, skin)
        VALUES ($1, $2, 'supergroup', 'cookiebot')
        ON CONFLICT (group_id) DO NOTHING
        """,
        group_id,
        f"New Group {group_id}",
        name="setlang_test_group",
    )


async def _delete_group(group_id: int) -> None:
    # ON DELETE CASCADE clears the group_configs row too, same as
    # qa/integration/factories.py's World.teardown.
    await db.execute(
        "DELETE FROM groups WHERE group_id = $1", group_id, name="setlang_test_group_cleanup"
    )


async def _fetch_language(group_id: int) -> str | None:
    row = await db.fetchrow(
        "SELECT language FROM group_configs WHERE group_id = $1",
        group_id,
        name="setlang_test_read_language",
    )
    return row["language"] if row is not None else None


def _bot_join_update(
    update_id: int, group_id: int, adder_id: int, language_code: str | None
) -> dict[str, Any]:
    """A `new_chat_members` update whose sole joiner is the bot itself, with the
    adder's Telegram `language_code` set (or entirely absent, matching v1's
    `'language_code' in msg['from']` gate) — the one shape
    `qa.conftest.make_join_update` cannot produce, since it never carries a
    `language_code` on the sender.
    """
    from_user: dict[str, Any] = {
        "id": adder_id,
        "is_bot": False,
        "first_name": "Adder",
        "username": "adder",
    }
    if language_code is not None:
        from_user["language_code"] = language_code
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": int(time.time()),
            "chat": {"id": group_id, "type": "supergroup", "title": f"New Group {group_id}"},
            "from": from_user,
            "new_chat_members": [
                {"id": _BOT_ID, "is_bot": True, "first_name": "Cookiebot", "username": BOT_USERNAME}
            ],
        },
    }


# No local `dispatcher` fixture: the suite drives `cb_gateway.main.dp`, the
# dispatcher the service actually serves. Building a private one also fails now
# that the router is registered — aiogram allows a router exactly one parent.


class Ctx:
    def __init__(self) -> None:
        self.group_id = _next_group_id()


@pytest.fixture
def setlang_ctx(
    database: ModuleType, run: Callable[[Coroutine[Any, Any, Any]], Any]
) -> Iterator[Ctx]:
    ctx = Ctx()
    run(_create_bare_group(ctx.group_id))
    yield ctx
    run(_delete_group(ctx.group_id))


# --------------------------------------------------------------------- given


@given("that the user is on the Cookiebot settings page")
def on_settings_page() -> None:
    """No web settings page exists in v1 or v2 — see the module docstring."""


@given("the user has access to the language settings")
def has_language_access() -> None:
    """Same as above: decorative context from the copied QA background."""


@given("that the user is on the language settings page")
def on_language_settings_page() -> None:
    """Same as above."""


@given("a brand new group with no stored language")
def brand_new_group(setlang_ctx: Ctx) -> None:
    """The fixture already created a bare `groups` row with no `group_configs`
    row; requesting it here only guarantees it exists before the join event."""


@given("Telegram will reject setMyCommands")
def telegram_rejects_set_my_commands(telegram: MockTelegram) -> None:
    telegram.fail("setMyCommands")


# ---------------------------------------------------------------------- when


@when(parsers.parse('they select "{language}" from the language options'))
def select_language_from_options(
    setlang_ctx: Ctx,
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
    language: str,
) -> None:
    """No web UI exists to drive; the closest real trigger for "the user's
    language becomes X" is a new group whose adder's own Telegram client is
    already set to that language (docs/contracts/core_setlang.md)."""
    code = _LANGUAGE_NAME_TO_TELEGRAM_CODE[language]
    feed(
        run,
        dispatcher,
        bot,
        _bot_join_update(next_update_id(), setlang_ctx.group_id, ADDER_ID, code),
    )


@when(parsers.parse('a user whose Telegram client language is "{code}" adds the bot to the group'))
def adder_with_language_adds_bot(
    setlang_ctx: Ctx,
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
    code: str,
) -> None:
    feed(
        run,
        dispatcher,
        bot,
        _bot_join_update(next_update_id(), setlang_ctx.group_id, ADDER_ID, code),
    )


@when("a user with no Telegram client language adds the bot to the group")
def adder_without_language_adds_bot(
    setlang_ctx: Ctx,
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
) -> None:
    feed(
        run,
        dispatcher,
        bot,
        _bot_join_update(next_update_id(), setlang_ctx.group_id, ADDER_ID, None),
    )


# ---------------------------------------------------------------------- then


@then(parsers.parse("the bot should display texts and respond in {language}"))
def bot_responds_in_language(
    setlang_ctx: Ctx, run: Callable[[Coroutine[Any, Any, Any]], Any], language: str
) -> None:
    stored = run(_fetch_language(setlang_ctx.group_id))
    assert stored is not None, "expected a language to have been derived and stored"
    assert locales.resolve_language(stored) == _LANGUAGE_NAME_TO_CANONICAL[language], stored


@then(parsers.parse('the group\'s stored language should be "{value}"'))
def stored_language_should_be(
    setlang_ctx: Ctx, run: Callable[[Coroutine[Any, Any, Any]], Any], value: str
) -> None:
    assert run(_fetch_language(setlang_ctx.group_id)) == value


@then("the group's stored language should be left unset")
def stored_language_should_be_unset(
    setlang_ctx: Ctx, run: Callable[[Coroutine[Any, Any, Any]], Any]
) -> None:
    assert run(_fetch_language(setlang_ctx.group_id)) is None


@then("the bot should relabel the group's command menu in Portuguese, Spanish and English scopes")
def command_menu_relabeled_in_all_scopes(setlang_ctx: Ctx, telegram: MockTelegram) -> None:
    scoped_codes = set()
    for call in telegram.calls_to("setMyCommands"):
        scope = json.loads(call.get("scope", "{}"))
        if scope.get("chat_id") == setlang_ctx.group_id:
            scoped_codes.add(call.get("language_code"))
    assert scoped_codes == {"pt", "es", "en"}, scoped_codes
