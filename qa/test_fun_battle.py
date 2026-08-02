"""Step definitions for fun_battle.

QA: qa/features/fun_battle.feature (synced from Cookiebot-QA/features/fun_battle.feature,
see that file's own header for why its one scenario is skipped rather than
asserted). Contract: docs/contracts/fun_battle.md. Design: .specs/features/fun_battle/.

Drives the real dispatcher (`cb_gateway.main.dp`, via `qa/conftest.py`'s
`dispatcher` fixture) against the mock Telegram API and a real `group_members`/
`users` pair, same pattern `qa/test_fun_ship.py` established for the same
registry: members are seeded through `cb_core.members.record`, because the
roster read *is* the feature (the redesign's whole point — see the module
docstring in `cb_gateway/handlers/battle.py`). `MockTelegram.set_profile_photo`
(new, added alongside this port) is the seam for `bot.get_user_profile_photos`,
the accepted redesign's replacement for v1's `telegram.me` scrape.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Iterator
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from pytest_bdd import given, scenarios, then, when

from cb_core import db, group_config, locales, members
from cb_core.members import MemberIdentity
from qa.conftest import GROUP_ID, USER_ID, Context, feed, make_message_update, next_update_id
from qa.mock_telegram import MockTelegram

scenarios("fun_battle.feature")

# An id range distinct from every other suite's seeded ids (fun_ship's
# 760_000_00x, everyone's 766_500_00x) so a scenario here can assert "the
# poll names this exact member" with no ambiguity.
SEEDED = (
    MemberIdentity(user_id=768_100_001, username="battler_one", first_name="One"),
    MemberIdentity(user_id=768_100_002, username="battler_two", first_name="Two"),
)

# qa/conftest.py:_user gives every mock sender this username.
SENDER_USERNAME = "tester"


@pytest.fixture(autouse=True)
def _reset(run: Callable[[Coroutine[Any, Any, Any]], Any]) -> Iterator[None]:
    """Same reset shape `qa/test_fun_ship.py` uses for the same registry."""
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
            "DELETE FROM group_members WHERE group_id = $1",
            GROUP_ID,
            name="qa_battle_clean_members",
        )
    )
    run(
        db.execute(
            "DELETE FROM users WHERE user_id = ANY($1::bigint[])",
            [m.user_id for m in SEEDED],
            name="qa_battle_clean_users",
        )
    )
    group_config._l1.clear()  # noqa: SLF001 - the L1 dict is the seam the harness owns


class Ctx:
    """Extra per-scenario state on top of qa/conftest.py's base `Context`."""

    def __init__(self) -> None:
        self.pending_text: str = ""

    def alloc_id(self) -> int:
        return next_update_id()


@pytest.fixture
def battle_ctx() -> Ctx:
    return Ctx()


def _seed_two(run: Callable[[Coroutine[Any, Any, Any]], Any], telegram: MockTelegram) -> None:
    """Wipes and re-seeds both members, with a profile photo each — the
    accepted redesign's `bot.get_user_profile_photos` seam. The wipe is not
    optional (`qa/test_fun_ship.py:_seed`'s identical note): `GROUP_ID` is
    shared across every acceptance suite.

    Also gives the sender (`USER_ID`/`"tester"`) a profile photo: the
    "random" pick draws from every registered member with a username, and
    the sender self-registers on the way in
    (`cb_gateway.handlers.members`) before `battle.py` ever runs — the same
    ordering `cb_core/members.py`'s own docstring documents for v1's
    `check_new_name`. Without this, a "random" scenario is flaky: whichever
    combination happens to include the sender would hit `battle_extract`
    instead of a successful battle.
    """
    run(
        db.execute("DELETE FROM group_members WHERE group_id = $1", GROUP_ID, name="qa_battle_wipe")
    )
    members.reset_cache()
    telegram.set_profile_photo(USER_ID, "photo_tester")
    for identity in SEEDED:
        run(members.record(GROUP_ID, identity))
        telegram.set_profile_photo(identity.user_id, f"photo_{identity.username}")


# --------------------------------------------------------------------- given


@given("that the bot is in the group and properly set up")
def bot_set_up(ctx: Context) -> None:
    ctx.bot_running = True


@given("that the user is a member of the group")
def user_is_member() -> None:
    """Nothing to arrange: the sender self-registers on the way in
    (`cb_gateway.handlers.members`), same as `fun_ship`'s identical note."""


@given("that two other members are registered in the group")
def two_other_members(
    database: Any, run: Callable[[Coroutine[Any, Any, Any]], Any], telegram: MockTelegram
) -> None:
    _seed_two(run, telegram)


@given("that no other members are registered in the group")
def no_other_members(database: Any, run: Callable[[Coroutine[Any, Any, Any]], Any]) -> None:
    run(
        db.execute(
            "DELETE FROM group_members WHERE group_id = $1", GROUP_ID, name="qa_battle_wipe_empty"
        )
    )
    members.reset_cache()


@given("the fun feature is turned off")
def fun_disabled(database: Any, run: Callable[[Coroutine[Any, Any, Any]], Any]) -> None:
    run(group_config.set_config(GROUP_ID, functions_fun=False))
    group_config._l1.clear()  # noqa: SLF001


# ---------------------------------------------------------------------- when


@when("the user types the command /battle")
def user_types_battle(battle_ctx: Ctx) -> None:
    """First half of the (skipped) QA scenario's two-line `/battle @tag` —
    see the feature file's header for why this scenario doesn't assert the
    literal outcome."""
    battle_ctx.pending_text = "/battle"


@when("tags another user in the group")
def user_tags_another(
    battle_ctx: Ctx,
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
) -> None:
    text = f"{battle_ctx.pending_text} @{SEEDED[0].username}"
    feed(run, dispatcher, bot, make_message_update(text, battle_ctx.alloc_id()))


@when("the user tags both other members in a /battle command")
def user_tags_both(
    battle_ctx: Ctx,
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
) -> None:
    text = f"/battle @{SEEDED[0].username} @{SEEDED[1].username}"
    feed(run, dispatcher, bot, make_message_update(text, battle_ctx.alloc_id()))


@when("the user sends the command /battle random")
def user_sends_random(
    battle_ctx: Ctx,
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
) -> None:
    feed(run, dispatcher, bot, make_message_update("/battle random", battle_ctx.alloc_id()))


@when("the user tags a stranger and a registered member in a /battle command")
def user_tags_stranger(
    battle_ctx: Ctx,
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
) -> None:
    # The stranger is checked first (design R2.3's ordered failure), so the
    # failure names them, not the registered member.
    text = f"/battle @nobody_has_seen_this_user @{SEEDED[0].username}"
    feed(run, dispatcher, bot, make_message_update(text, battle_ctx.alloc_id()))


@when("the user sends the command /battle")
def user_sends_bare(
    battle_ctx: Ctx,
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
) -> None:
    feed(run, dispatcher, bot, make_message_update("/battle", battle_ctx.alloc_id()))


# ---------------------------------------------------------------------- then


def _last_text(telegram: MockTelegram) -> str:
    sent = telegram.calls_to("sendMessage")
    assert sent, "expected a sendMessage call, got none"
    return str(sent[-1].get("text", ""))


@then(
    'the bot should display a message "Who would win in a battle?" with options "Option A" and "Option B"'
)
def qa_scenario_not_yet_reachable(telegram: MockTelegram) -> None:
    """v1's one-tag path needs the `Fight/` GCS bucket export — the same
    prerequisite `fun_death`'s `Death/` prefix is blocked on
    (`.specs/features/fun_death/spec.md`, `.specs/features/fun_battle/spec.md`).
    Not implemented in this slice; skipped rather than asserting an outcome
    that does not exist yet or silently repurposing the QA wording to check
    something else.
    """
    pytest.skip(
        "fun_battle's one-tag path is blocked on the Fight/ GCS bucket export, "
        "same prerequisite as fun_death — see .specs/features/fun_battle/spec.md"
    )


@then("makes a poll in which the users can vote on who would win in a battle")
def qa_scenario_poll_unreached(telegram: MockTelegram) -> None:
    """Never runs — the previous `Then` step already skipped the scenario.
    Exists only so pytest-bdd has a step function to bind this line to."""


@then("the bot should post a two-photo battle and a poll naming both tagged members")
def bot_posts_two_person_battle(telegram: MockTelegram) -> None:
    media_calls = telegram.calls_to("sendMediaGroup")
    assert media_calls, "expected a sendMediaGroup call"
    poll_calls = telegram.calls_to("sendPoll")
    assert poll_calls, "expected a sendPoll call"
    options = poll_calls[-1].get("options", "")
    assert SEEDED[0].username in options, options
    assert SEEDED[1].username in options, options
    assert poll_calls[-1].get("is_anonymous") == "false", poll_calls[-1]
    assert poll_calls[-1].get("allows_multiple_answers") == "false", poll_calls[-1]


@then("the bot should post a two-photo battle and a poll naming two registered members")
def bot_posts_random_battle(telegram: MockTelegram) -> None:
    media_calls = telegram.calls_to("sendMediaGroup")
    assert media_calls, "expected a sendMediaGroup call"
    poll_calls = telegram.calls_to("sendPoll")
    assert poll_calls, "expected a sendPoll call"
    options = poll_calls[-1].get("options", "")
    named = {SEEDED[0].username, SEEDED[1].username, SENDER_USERNAME}
    assert any(username in options for username in named), options


@then("the bot should reply that not enough members are known to battle")
def bot_says_battle_no(telegram: MockTelegram) -> None:
    assert _last_text(telegram) == locales.get("battle_no", "en")
    assert not telegram.calls_to("sendPoll")


@then("the bot should reply that it could not extract the stranger's photo")
def bot_says_battle_extract(telegram: MockTelegram) -> None:
    body = _last_text(telegram)
    assert "nobody_has_seen_this_user" in body, body
    assert not telegram.calls_to("sendPoll")


@then("the bot should reply that a profile picture is needed")
def bot_says_battle_no_picture(telegram: MockTelegram) -> None:
    assert _last_text(telegram) == locales.get("battle_no_picture", "en")
    assert not telegram.calls_to("sendPoll")


@then("the bot should reply with a message saying that the fun feature is turned off")
def bot_says_fun_off(telegram: MockTelegram) -> None:
    assert _last_text(telegram) == locales.get("fun_off", "en")
    assert not telegram.calls_to("sendPoll")
