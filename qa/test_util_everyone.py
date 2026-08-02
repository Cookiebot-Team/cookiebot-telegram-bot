"""Step definitions for util_everyone.

QA: qa/features/util_everyone.feature (synced from Cookiebot-QA/features/util_everyone.feature,
plus one net-new scenario for the "fewer than two known members" path — see that
file's own header). Contract: docs/contracts/util_everyone.md.

Drives the real dispatcher (`cb_gateway.main.dp`, via `qa/conftest.py`'s
`dispatcher` fixture) against the mock Telegram API and a real `group_members` /
`users` pair, because the roster read *is* the feature (D-EV-1): the handler
reads it through `cb_core.members.roster`, and AGENTS.md §6 forbids mocking our
own code in an acceptance test. Members are seeded through `cb_core.members.record`
— the same call the gateway's own bookkeeping handler makes on every message —
mirroring `qa/test_fun_ship.py`'s pattern for the same registry.

The gateway->worker enqueue (`cb_gateway.queue.enqueue`) *is* the outside world
here: the arq broker it talks to is exactly the kind of thing AGENTS.md §6 says
to mock. `fake_queue` below monkeypatches
`cb_gateway.handlers.everyone`'s own reference to it (not `cb_gateway.queue`
itself), the same seam the handler imports it through.

QA's own wording phrases the trigger as "/ping everyone", which has no v1
equivalent at all (this file's header, mirroring fun_dice's identical note for
"roll 6"). Unlike fun_dice's `_to_command` (which patches a parameterised bare
word), the QA phrase here is a fixed literal, so both `when` steps below just
send the real v1 trigger "/everyone" directly, leaving the Gherkin wording
untouched.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Iterator
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from pytest_bdd import given, scenarios, then, when

from cb_core import db, jobs, locales, members
from cb_core.members import MemberIdentity
from cb_gateway.handlers import everyone as everyone_handler
from qa.conftest import GROUP_ID, USER_ID, Context, feed, make_message_update, next_update_id
from qa.mock_telegram import MockTelegram

scenarios("util_everyone.feature")

Run = Callable[[Coroutine[Any, Any, Any]], Any]

# An id range distinct from every other suite's seeded ids (fun_ship's
# 760_000_00x, calladms' ADMIN_ID/SECOND_ADMIN_ID) so a scenario here can assert
# "the ping names this exact member" with no ambiguity.
EXTRA_MEMBER = MemberIdentity(user_id=766_500_001, username="everyone_seed_one", first_name="Extra")

# qa/conftest.py:_user gives every mock sender this username, and
# cb_gateway.handlers.members registers the sender on the way in — the same
# self-registration v1's check_new_name performed before dispatch
# (UserRegisters.py:64-88, docstring of cb_core/members.py).
SENDER_USERNAME = "tester"


@pytest.fixture(autouse=True)
def fake_queue(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    """Substitutes `cb_gateway.handlers.everyone`'s `enqueue` reference with a
    fake queue. The arq broker `cb_gateway.queue.enqueue` talks to is the
    outside world (AGENTS.md §6); the handler itself is not mocked at all.

    Autouse, and patched before any step runs, so it is in place before the
    `when` step that triggers the handler — a scenario's `then` step only reads
    back what was recorded, it never sets the patch up itself.
    """
    calls: list[tuple[str, dict[str, Any]]] = []

    async def _fake_enqueue(job: str, *args: object, **kwargs: object) -> bool:
        calls.append((job, kwargs))
        return True

    monkeypatch.setattr(everyone_handler, "enqueue", _fake_enqueue)
    return calls


@pytest.fixture(autouse=True)
def _reset(run: Run) -> Iterator[None]:
    """`members`' write-skip caches are process-global; clear them so a scenario
    that just wiped `group_members` can re-seed the same identity (mirrors
    `qa/test_fun_ship.py`'s identical reset)."""
    members.reset_cache()
    yield
    members.reset_cache()
    try:
        db.pool()
    except RuntimeError:
        return  # no database in this run; nothing was ever persisted either
    run(
        db.execute(
            "DELETE FROM group_members WHERE group_id = $1",
            GROUP_ID,
            name="qa_everyone_clean_members",
        )
    )
    run(
        db.execute(
            "DELETE FROM users WHERE user_id = $1",
            EXTRA_MEMBER.user_id,
            name="qa_everyone_clean_users",
        )
    )


class Ctx:
    """Extra per-scenario state on top of qa/conftest.py's base `Context`."""

    def __init__(self) -> None:
        self.message_id: int | None = None

    def alloc_id(self) -> int:
        # Shared process-wide counter: a per-scenario counter collides with
        # earlier scenarios and the dedupe middleware drops the update.
        return next_update_id()


@pytest.fixture
def everyone_ctx() -> Ctx:
    return Ctx()


# --------------------------------------------------------------------- given


@given("that the bot is in the group and properly set up")
def bot_set_up(ctx: Context) -> None:
    ctx.bot_running = True


@given("that the user is an admin of the group")
def user_is_admin(telegram: MockTelegram, database: Any, run: Run) -> None:
    """Makes the default sender (`USER_ID`, same as every `when` step below) an
    admin, and seeds one extra known member. Together with the sender's own
    self-registration on the way in (`cb_gateway.handlers.members`), the roster
    has two known usernames by the time the handler reads it — v1's `< 2` gate
    (`UserRegisters.py:107`) needs at least that to reach the ping at all.

    The wipe before seeding is not optional: `GROUP_ID` is shared by every
    acceptance suite, several of which write `group_members` rows and do not
    clean them up (`qa/test_fun_ship.py`'s `_seed` docstring makes the same
    point). Leftovers from an earlier suite would inflate "known users" past 2
    and desync it from what this scenario actually asserts.
    """
    run(
        db.execute(
            "DELETE FROM group_members WHERE group_id = $1", GROUP_ID, name="qa_everyone_seed_wipe"
        )
    )
    telegram.set_admins(GROUP_ID, [(USER_ID, "administrator")])
    run(members.record(GROUP_ID, EXTRA_MEMBER))


@given("that the user is not an admin of the group")
def user_is_not_admin() -> None:
    """Nothing to arrange: `qa/conftest.py`'s autouse `_clean` fixture already
    seeds only `ADMIN_ID` as an admin, and the sender in every `when` step below
    is `USER_ID` — a confirmed non-admin by default."""


@given("that the group has fewer than two known members")
def fewer_than_two_known_members(database: Any, run: Run) -> None:
    """Undoes the extra member the admin `given` step above just seeded, so the
    roster is empty going into the `when` step. The sender still self-registers
    on the way in, leaving exactly one known username — one short of v1's `< 2`
    gate (`UserRegisters.py:107`), the same boundary
    `qa/test_fun_ship.py`'s "nobody else has spoken yet" scenario exercises for
    its own `no_ship` path.
    """
    run(
        db.execute(
            "DELETE FROM group_members WHERE group_id = $1",
            GROUP_ID,
            name="qa_everyone_wipe_members",
        )
    )


# ---------------------------------------------------------------------- when


@when("an admin sends the command to /ping everyone")
def admin_sends_everyone(run: Run, dispatcher: Dispatcher, bot: Bot, everyone_ctx: Ctx) -> None:
    """QA phrases this trigger as "/ping everyone" (module docstring); v1's real
    trigger is "/everyone", and that is what actually reaches the dispatcher."""
    update_id = everyone_ctx.alloc_id()
    everyone_ctx.message_id = update_id
    feed(run, dispatcher, bot, make_message_update("/everyone", update_id, user_id=USER_ID))


@when("a non-admin sends the command to /ping everyone")
def non_admin_sends_everyone(run: Run, dispatcher: Dispatcher, bot: Bot, everyone_ctx: Ctx) -> None:
    update_id = everyone_ctx.alloc_id()
    everyone_ctx.message_id = update_id
    feed(run, dispatcher, bot, make_message_update("/everyone", update_id, user_id=USER_ID))


# ---------------------------------------------------------------------- then


def _group_calls(telegram: MockTelegram) -> list[dict[str, Any]]:
    return [c for c in telegram.calls_to("sendMessage") if int(c.get("chat_id", 0)) == GROUP_ID]


@then("all members of the group should receive a notification")
def bot_pings_everyone(
    telegram: MockTelegram,
    fake_queue: list[tuple[str, dict[str, Any]]],
    everyone_ctx: Ctx,
) -> None:
    calls = _group_calls(telegram)
    assert calls, "expected a group-facing sendMessage call"
    body = "\n".join(str(c.get("text", "")) for c in calls)
    # D-EV-4: the English "Number of known users:" header, first chunk only.
    assert "Number of known users: 2\n" in body, body
    assert f"@{SENDER_USERNAME}" in body, body
    assert f"@{EXTRA_MEMBER.username}" in body, body

    # R4.7: exactly one EVERYONE_FANOUT job, scalars only, never a DM sent here.
    assert len(fake_queue) == 1, fake_queue
    job, kwargs = fake_queue[0]
    assert job == jobs.EVERYONE_FANOUT
    assert kwargs["group_id"] == GROUP_ID
    assert kwargs["chat_id"] == GROUP_ID
    assert kwargs["message_id"] == everyone_ctx.message_id
    assert kwargs["chat_title"] == "QA Group"
    assert kwargs["lang"] == "en"
    dm_calls = [c for c in telegram.calls_to("sendMessage") if int(c.get("chat_id", 0)) != GROUP_ID]
    assert not dm_calls, dm_calls


@then(
    "the bot should respond with a message indicating that they do not have permission to use this command"
)
def bot_denies_non_admin(
    telegram: MockTelegram, fake_queue: list[tuple[str, dict[str, Any]]]
) -> None:
    calls = _group_calls(telegram)
    assert calls, "expected a group-facing sendMessage call"
    assert calls[-1].get("text", "") == locales.get("everyone_no", "en")
    assert not fake_queue, fake_queue


@then("the bot should respond with a message indicating that not enough members are known yet")
def bot_says_too_few_known(
    telegram: MockTelegram, fake_queue: list[tuple[str, dict[str, Any]]]
) -> None:
    calls = _group_calls(telegram)
    assert calls, "expected a group-facing sendMessage call"
    assert calls[-1].get("text", "") == locales.get("everyone_len", "en")
    assert not fake_queue, fake_queue
