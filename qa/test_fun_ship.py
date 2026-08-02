"""Step definitions for fun_ship.

QA: qa/features/fun_ship.feature (synced from Cookiebot-QA/features/fun_ship.feature,
with one Then line corrected to v1's real behaviour — see that file's header).
Contract: docs/contracts/fun_ship.md.

Drives the real dispatcher (`cb_gateway.main.dp`, via `qa/conftest.py`'s
`dispatcher` fixture) against the mock Telegram API and a real `group_members` /
`users` pair, because that registry *is* the feature: `cb_gateway.handlers.ship`
reads it through `cb_core.members`, and AGENTS.md §6 forbids mocking our own code
in an acceptance test. Members are seeded through `cb_core.members.record` — the
same call the gateway's own bookkeeping handler makes on every message — the way
`qa/test_fun_random.py` seeds the media pool through `storage.media().put`.

The seeded members need distinct usernames, and `qa/conftest.py:_user` gives
every mock sender the same one ("tester"), so the registry is populated directly
rather than by feeding messages from N fake senders.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Coroutine, Iterator
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from pytest_bdd import given, parsers, scenarios, then, when

from cb_core import db, group_config, locales, members
from cb_core.members import MemberIdentity
from qa.conftest import GROUP_ID, Context, feed, make_message_update, next_update_id
from qa.mock_telegram import MockTelegram

scenarios("fun_ship.feature")

# Seeded members. Ids are outside every range qa/conftest.py hands out so a
# scenario can assert "the reply names two of *these*" without ambiguity.
SEEDED = (
    MemberIdentity(user_id=760_000_001, username="shipmate_one", first_name="One"),
    MemberIdentity(user_id=760_000_002, username="shipmate_two", first_name="Two"),
    MemberIdentity(user_id=760_000_003, username="shipmate_three", first_name="Three"),
)

_MENTION = re.compile(r"@([A-Za-z0-9_]+)")

# qa/conftest.py:_user gives every mock sender this username.
SENDER_USERNAME = "tester"


@pytest.fixture(autouse=True)
def _reset(run: Callable[[Coroutine[Any, Any, Any]], Any]) -> Iterator[None]:
    """`members`' write-skip caches and `group_config._l1` are both
    process-global; the "fun off" scenario flips a flag on the shared GROUP_ID
    (same reset `qa/test_fun_random.py` performs for its own gate)."""
    members.reset_cache()
    yield
    members.reset_cache()
    try:
        db.pool()
    except RuntimeError:
        return
    run(group_config.set_config(GROUP_ID, functions_fun=True))
    run(
        db.execute(
            "DELETE FROM group_members WHERE group_id = $1", GROUP_ID, name="qa_ship_clean_members"
        )
    )
    run(
        db.execute(
            "DELETE FROM users WHERE user_id = ANY($1::bigint[])",
            [m.user_id for m in SEEDED],
            name="qa_ship_clean_users",
        )
    )
    group_config._l1.clear()  # noqa: SLF001 - the L1 dict is the seam the harness owns


class Ctx:
    def alloc_id(self) -> int:
        return next_update_id()


@pytest.fixture
def ship_ctx() -> Ctx:
    return Ctx()


# --------------------------------------------------------------------- given


@given("that the bot is in the group and properly set up")
def bot_set_up(ctx: Context) -> None:
    ctx.bot_running = True


def _seed(run: Callable[[Coroutine[Any, Any, Any]], Any], count: int) -> None:
    """Seeds exactly `count` members into an empty group.

    The wipe is not optional. `GROUP_ID` is shared by every acceptance suite
    (`qa/conftest.py`'s own note about it), and several of them write
    `group_members` rows — a leftover member with a `users` row is a legitimate
    ship target, so "the reply names two of the ones I seeded" would fail
    whenever this suite ran after one of those. Scenario order is not something
    a scenario should depend on.
    """
    run(
        db.execute(
            "DELETE FROM group_members WHERE group_id = $1", GROUP_ID, name="qa_ship_seed_wipe"
        )
    )
    members.reset_cache()
    for identity in SEEDED[:count]:
        run(members.record(GROUP_ID, identity))


@given("that the group has registered members")
def group_has_members(database: Any, run: Callable[[Coroutine[Any, Any, Any]], Any]) -> None:
    _seed(run, len(SEEDED))


@given("that nobody else in the group has spoken yet")
def nobody_else_has_spoken(database: Any, run: Callable[[Coroutine[Any, Any, Any]], Any]) -> None:
    """Seeds nothing. The sender registers themselves on the way in (that is
    what `cb_gateway.handlers.members` is for, and v1's `check_new_name` ran on
    the same trigger), so the group ends up with exactly one member — v1's
    `except IndexError` arm."""
    _seed(run, 0)


@given("the fun feature is turned off")
def fun_disabled(database: Any, run: Callable[[Coroutine[Any, Any, Any]], Any]) -> None:
    run(group_config.set_config(GROUP_ID, functions_fun=False))
    group_config._l1.clear()  # noqa: SLF001


# ---------------------------------------------------------------------- when


@when(parsers.parse("the user sends the command {command}"))
def user_sends_command(
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
    ship_ctx: Ctx,
    command: str,
) -> None:
    feed(run, dispatcher, bot, make_message_update(command, ship_ctx.alloc_id()))


# ---------------------------------------------------------------------- then


def _last_text(telegram: MockTelegram) -> str:
    sent = telegram.calls_to("sendMessage")
    assert sent, "expected a sendMessage call, got none"
    return str(sent[-1].get("text", ""))


@then("the bot should reply with a shipp of two users in the group")
def bot_ships_two_members(telegram: MockTelegram) -> None:
    body = _last_text(telegram)
    mentioned = set(_MENTION.findall(body))
    # The sender counts: `cb_gateway.handlers.members` registers them on the way
    # in, exactly as v1's `check_new_name` did before dispatch, so "@tester" is
    # as legitimate a target as anyone seeded.
    registered = {m.username for m in SEEDED if m.username} | {SENDER_USERNAME}
    assert len(mentioned & registered) == 2, (mentioned, body)
    # The whole v1 template, not just the two names: dynamics, children and the
    # divorce percentage all have to survive the port.
    assert "%(" not in body
    for fragment in ("Dynamics:", "Children:", "Chance of divorce:"):
        assert fragment in body, (fragment, body)


@then(parsers.parse("the bot should reply with a shipp of {name_a} and {name_b}"))
def bot_ships_named(telegram: MockTelegram, name_a: str, name_b: str) -> None:
    """Two explicit arguments are used verbatim and never looked up — v1 ships
    strangers happily (`UserRegisters.py:219-221`)."""
    body = _last_text(telegram)
    assert f"@{name_a}" in body, body
    assert f"@{name_b}" in body, body


@then("the bot should reply with a message saying that the fun feature is turned off")
def bot_says_fun_off(telegram: MockTelegram) -> None:
    assert _last_text(telegram) == locales.get("fun_off", "en")


@then("the bot should reply with a message saying it has not seen enough members")
def bot_says_no_ship(telegram: MockTelegram) -> None:
    assert _last_text(telegram) == locales.get("no_ship", "en")


@then("the user receives no response")
def no_response(telegram: MockTelegram) -> None:
    assert not telegram.calls_to("sendMessage")
