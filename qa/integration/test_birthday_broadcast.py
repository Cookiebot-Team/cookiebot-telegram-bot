"""`cb_core.birthdays.groups_with_birthdays` against a real Citus.

The daily broadcast's first question — "which groups have a birthday today?"
— replaces v1's one-HTTP-round-trip-per-group-per-day sweep
(`../COOKIEBOT-Telegram-Group-Bot/Bot/Birthdays.py:20-39`). It is deliberately
cross-shard (a scheduled worker job, AGENTS.md §4.4), so what matters is that
it is *correct* and that the join stays node-local: `users` is a reference
table, replicated everywhere.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

import pytest

from cb_core import birthdays, db
from qa.integration.factories import World

pytestmark = pytest.mark.integration

Run = Callable[[Coroutine[Any, Any, Any]], Any]


def _set_birthday(run: Run, user_id: int, month: int, day: int) -> None:
    """`birth_month`/`birth_day` are generated columns (migration 0001), so the
    only way to set them is to set `birthdate` — which is also the only way the
    importer sets them, and therefore the shape worth testing. The year is
    irrelevant to the lookup and deliberately not today's."""
    run(
        db.execute(
            "UPDATE users SET birthdate = make_date(1990, $2, $3) WHERE user_id = $1",
            user_id,
            month,
            day,
            name="test_set_birthday",
        )
    )


def test_a_group_with_a_birthday_today_is_found(world: World, run: Run) -> None:
    member = world.add_user()
    _set_birthday(run, member.user_id, 3, 14)
    assert world.group_id in run(birthdays.groups_with_birthdays(3, 14))


def test_a_group_with_no_birthday_today_is_not(world: World, run: Run) -> None:
    member = world.add_user()
    _set_birthday(run, member.user_id, 3, 14)
    assert world.group_id not in run(birthdays.groups_with_birthdays(3, 15))


def test_a_member_who_left_does_not_keep_the_group_in_the_sweep(world: World, run: Run) -> None:
    """`left_at IS NULL`, the same predicate `members_with_birthday` uses — a
    group whose only birthday person has left must not get a post naming
    nobody."""
    member = world.add_user()
    _set_birthday(run, member.user_id, 3, 14)
    run(
        db.execute(
            "UPDATE group_members SET left_at = now() WHERE group_id = $1 AND user_id = $2",
            world.group_id,
            member.user_id,
            name="test_mark_left",
        )
    )
    assert world.group_id not in run(birthdays.groups_with_birthdays(3, 14))


def test_a_group_appears_once_however_many_people_share_the_day(world: World, run: Run) -> None:
    """One post per group, not one per birthday person — the sweep enqueues
    per group and the collage seats everyone."""
    for member in world.add_users(3):
        _set_birthday(run, member.user_id, 3, 14)
    found = run(birthdays.groups_with_birthdays(3, 14))
    assert found.count(world.group_id) == 1


# ------------------------------------------------------------------ plan shape


async def _plan_under_no_seqscan(sql: str, *args: object) -> str:
    """`EXPLAIN` the statement with sequential scans priced out of the way.

    The discriminator has to work on a table with a handful of rows, where a
    sequential scan is genuinely the cheapest plan and would be chosen whether
    or not the index is usable. `enable_seqscan = off` removes that ambiguity:
    the planner then picks the index **if it is allowed to**, and falls back to
    the (heavily penalised, but only remaining) sequential scan if it is not.
    So the plan tells us about index *eligibility*, not about this dataset.

    `citus.propagate_set_commands = 'local'` is what carries the `SET LOCAL`
    down to the shard queries; without it the coordinator's setting never
    reaches the node that plans the scan, and the assertion below reads a plan
    that was never influenced at all.
    """
    async with db.transaction() as conn:
        await conn.execute("SET LOCAL citus.propagate_set_commands = 'local'")
        await conn.execute("SET LOCAL enable_seqscan = off")
        rows = await conn.fetch(f"EXPLAIN (COSTS OFF) {sql}", *args)
    return "\n".join(str(row[0]) for row in rows)


class TestPartialIndexIsReachable:
    """`users_birthday_idx` is partial (`WHERE birthdate IS NOT NULL`), so every
    read has to state that predicate or the index is silently unusable.

    Nothing about the *results* changes when it is missing — `birth_month` is
    `EXTRACT(MONTH FROM birthdate)`, so a NULL birthdate can never match — which
    is exactly why this needs a plan-shape test rather than a behavioural one.
    Measured on 200k seeded users, single-node Citus: 318ms without, 38ms with.
    """

    def test_the_all_users_read_uses_the_birthday_index(self, run: Run) -> None:
        plan = run(_plan_under_no_seqscan(birthdays._ALL_USERS_WITH_BIRTHDAY, 3, 14))  # noqa: SLF001
        assert "users_birthday_idx" in plan, plan

    def test_the_daily_sweep_uses_the_birthday_index(self, run: Run) -> None:
        plan = run(_plan_under_no_seqscan(birthdays._GROUPS_WITH_BIRTHDAYS, 3, 14))  # noqa: SLF001
        assert "users_birthday_idx" in plan, plan

    def test_dropping_the_predicate_really_would_lose_the_index(self, run: Run) -> None:
        """The test above is only worth anything if the same statement without
        `birthdate IS NOT NULL` fails it — otherwise it would pass no matter
        what the module did."""
        without = birthdays._ALL_USERS_WITH_BIRTHDAY.replace(  # noqa: SLF001
            "WHERE birthdate IS NOT NULL\n   AND birth_month", "WHERE birth_month"
        )
        assert "birthdate" not in without, without
        plan = run(_plan_under_no_seqscan(without, 3, 14))
        assert "users_birthday_idx" not in plan, plan
