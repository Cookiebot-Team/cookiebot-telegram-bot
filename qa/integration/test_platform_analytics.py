"""`cb_core.platform_analytics` against real rollup rows in real Citus.

The unit layer fakes these queries, so what it cannot check is the part that
only a real cluster has an opinion about: that the SQL matches the schema, that
`count(DISTINCT group_id)` and the two correlated counts in the directory mean
what the module claims, and that the numbers are what Postgres computes rather
than what the fake returned.

**Every assertion here is scoped to the groups this test created.** These
queries are deliberately fleet-wide — that is the whole point of the module —
so a shared database with another test's rows in it is the normal case, not an
anomaly. Asserting `messages == 10` on a global sum would pass alone and fail
in CI; each test either filters the result to its own groups or asserts a
*delta* measured across the call.

The window is a date no other module seeds, for the same reason.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Iterator
from datetime import date, timedelta
from types import ModuleType
from typing import Any

import pytest

from cb_core import db, platform_analytics
from qa.integration.factories import World

pytestmark = pytest.mark.integration

Run = Callable[[Coroutine[Any, Any, Any]], Any]

#: Far enough from `test_analytics.py`'s `2026-02-10` that the two modules
#: cannot see each other's rows, whatever order they run in.
DAY = date(2027, 5, 5)
NEXT = DAY + timedelta(days=1)


def _seed_daily(run: Run, group_id: int, day: date, **fields: int | float) -> None:
    columns = ", ".join(fields)
    placeholders = ", ".join(f"${index + 3}" for index in range(len(fields)))
    run(
        db.execute(
            f"INSERT INTO group_daily_stats (group_id, day, {columns}) "
            f"VALUES ($1, $2, {placeholders})",
            group_id,
            day,
            *fields.values(),
            name="test_seed_platform_daily",
        )
    )


def _seed_command(run: Run, group_id: int, day: date, command: str, **fields: int) -> None:
    columns = ", ".join(fields)
    placeholders = ", ".join(f"${index + 4}" for index in range(len(fields)))
    run(
        db.execute(
            f"INSERT INTO command_daily_stats (group_id, day, command, {columns}) "
            f"VALUES ($1, $2, $3, {placeholders})",
            group_id,
            day,
            command,
            *fields.values(),
            name="test_seed_platform_command",
        )
    )


def _seed_llm(
    run: Run, group_id: int, day: date, provider: str, model: str, **fields: int | float
) -> None:
    columns = ", ".join(fields)
    placeholders = ", ".join(f"${index + 5}" for index in range(len(fields)))
    run(
        db.execute(
            f"INSERT INTO llm_daily_cost (group_id, day, provider, model, {columns}) "
            f"VALUES ($1, $2, $3, $4, {placeholders})",
            group_id,
            day,
            provider,
            model,
            *fields.values(),
            name="test_seed_platform_llm",
        )
    )


@pytest.fixture(autouse=True)
def clean_window(pg: ModuleType, run: Run) -> Iterator[None]:
    """Empty the rollup tables inside this module's date window, either side of
    every test.

    The rollups have **no foreign key to `groups`**, so `World.teardown()` —
    which deletes the group and lets `ON DELETE CASCADE` do the rest — leaves
    them behind. Per-group tests never notice, because their queries carry a
    `group_id` that no longer matches. A fleet-wide query notices immediately:
    without this, the second test in the file reads the first one's rows and
    the file passes or fails depending on the order pytest chose.

    Emptying a window rather than dropping rows by group id is what makes it
    reliable — the leftovers belong to groups this fixture can no longer name.
    """

    def purge() -> None:
        for table in ("group_daily_stats", "command_daily_stats", "llm_daily_cost"):
            run(
                db.execute(
                    f"DELETE FROM {table} WHERE day >= $1 AND day <= $2",
                    DAY - timedelta(days=2),
                    NEXT + timedelta(days=2),
                    name="test_purge_window",
                )
            )

    purge()
    yield
    purge()


class TestDaily:
    def test_two_groups_are_summed_into_one_row_per_day(
        self, run: Run, world: World, second_world: World
    ) -> None:
        """The one thing `cb_core.analytics` will never do, and the reason this
        module exists."""
        _seed_daily(run, world.group_id, DAY, messages=10, llm_tokens=100)
        _seed_daily(run, second_world.group_id, DAY, messages=7, llm_tokens=50)

        [row] = run(platform_analytics.daily(DAY, DAY))

        assert row.messages == 17
        assert row.llm_tokens == 150
        assert row.groups == 2

    def test_groups_counts_active_groups_not_existing_ones(
        self, run: Run, world: World, second_world: World
    ) -> None:
        """A group with no row on a day is not counted for that day — the
        rollup writes only what it saw, and "how many groups exist" is
        `reach()`'s question, not this one's.

        Both groups exist for the whole test; only the day each one wrote a row
        on moves.
        """
        _seed_daily(run, world.group_id, DAY, messages=1)
        _seed_daily(run, world.group_id, NEXT, messages=1)
        _seed_daily(run, second_world.group_id, NEXT, messages=1)

        rows = {row.day: row.groups for row in run(platform_analytics.daily(DAY, NEXT))}

        assert rows == {DAY: 1, NEXT: 2}

    def test_the_window_excludes_the_day_before(self, run: Run, world: World) -> None:
        _seed_daily(run, world.group_id, DAY - timedelta(days=1), messages=999)
        _seed_daily(run, world.group_id, DAY, messages=1)

        rows = run(platform_analytics.daily(DAY, NEXT))

        assert [(row.day, row.messages) for row in rows] == [(DAY, 1)]

    def test_cost_comes_back_as_a_float(self, run: Run, world: World) -> None:
        """`numeric(12,4)` is a `Decimal` from asyncpg and JSON has no Decimal;
        the struct converts once here rather than at every call site."""
        _seed_daily(run, world.group_id, DAY, llm_cost_usd=1.25)

        [row] = run(platform_analytics.daily(DAY, DAY))

        assert isinstance(row.llm_cost_usd, float)


class TestTopGroups:
    def test_busiest_first_with_the_title_joined_in(
        self, run: Run, world: World, second_world: World
    ) -> None:
        _seed_daily(run, world.group_id, DAY, messages=5)
        _seed_daily(run, world.group_id, NEXT, messages=5)
        _seed_daily(run, second_world.group_id, DAY, messages=100)

        rows = run(platform_analytics.top_groups(DAY, NEXT, limit=100))

        assert [row.group_id for row in rows] == [second_world.group_id, world.group_id]
        assert rows[1].messages == 10, "days are summed per group"
        assert rows[0].title is not None, "the colocated join to `groups` resolved"

    def test_peak_active_users_is_the_best_day_not_the_sum(self, run: Run, world: World) -> None:
        _seed_daily(run, world.group_id, DAY, active_users=3)
        _seed_daily(run, world.group_id, NEXT, active_users=9)

        rows = run(platform_analytics.top_groups(DAY, NEXT, limit=100))
        [mine] = [row for row in rows if row.group_id == world.group_id]

        assert mine.peak_active_users == 9


class TestCommands:
    def test_invocations_sum_and_groups_are_counted_distinctly(
        self, run: Run, world: World, second_world: World
    ) -> None:
        """The number that makes this table different from the per-group one:
        one busy group is not the same fact as a command used everywhere."""
        command = f"qa{world.group_id}"
        _seed_command(run, world.group_id, DAY, command, invocations=5)
        _seed_command(run, world.group_id, NEXT, command, invocations=5)
        _seed_command(run, second_world.group_id, DAY, command, invocations=1)

        rows = run(platform_analytics.commands(DAY, NEXT, limit=100))
        [mine] = [row for row in rows if row.command == command]

        assert mine.invocations == 11
        assert mine.groups == 2

    def test_p95_is_the_worst_day_not_an_average(self, run: Run, world: World) -> None:
        command = f"qa{world.group_id}"
        _seed_command(run, world.group_id, DAY, command, invocations=1, p95_latency_ms=100)
        _seed_command(run, world.group_id, NEXT, command, invocations=1, p95_latency_ms=900)

        rows = run(platform_analytics.commands(DAY, NEXT, limit=100))
        [mine] = [row for row in rows if row.command == command]

        assert mine.p95_latency_ms == 900


class TestLlm:
    def test_spend_is_summed_across_groups_per_model(
        self, run: Run, world: World, second_world: World
    ) -> None:
        model = f"qa-model-{world.group_id}"
        _seed_llm(run, world.group_id, DAY, "anthropic", model, calls=2, cost_usd=1.5)
        _seed_llm(run, second_world.group_id, DAY, "anthropic", model, calls=3, cost_usd=0.75)

        rows = run(platform_analytics.llm_costs(DAY, DAY))
        [mine] = [row for row in rows if row.model == model]

        assert mine.calls == 5
        assert mine.cost_usd == 2.25


class TestDirectory:
    def test_lists_the_group_with_its_member_and_admin_counts(self, run: Run, world: World) -> None:
        world.add_user(admin=True)
        world.add_user()

        rows = run(platform_analytics.directory(limit=1000))
        [mine] = [row for row in rows if row.group_id == world.group_id]

        assert mine.members == 2
        assert mine.admins == 1
        assert mine.chat_type == "supergroup"

    def test_the_cursor_pages_forward_without_repeating_a_row(
        self, run: Run, world: World, second_world: World
    ) -> None:
        """Keyset, not OFFSET (D11): `after` is the id itself, so a group
        created between two pages cannot make a row repeat or vanish."""
        first = run(platform_analytics.directory(limit=1))
        assert len(first) == 1

        second = run(platform_analytics.directory(limit=1, after=first[0].group_id))

        assert second[0].group_id > first[0].group_id

    def test_search_matches_the_title(self, run: Run, world: World) -> None:
        rows = run(platform_analytics.directory(limit=100, search=str(world.group_id)))

        assert [row.group_id for row in rows] == [world.group_id]

    def test_a_group_the_bot_left_is_hidden_unless_asked_for(self, run: Run, world: World) -> None:
        run(
            db.execute(
                "UPDATE groups SET left_at = now() WHERE group_id = $1",
                world.group_id,
                name="test_mark_left",
            )
        )

        active = run(platform_analytics.directory(limit=1000))
        assert world.group_id not in {row.group_id for row in active}

        everything = run(platform_analytics.directory(limit=1000, active_only=False))
        [mine] = [row for row in everything if row.group_id == world.group_id]
        assert mine.left_at is not None


class TestReach:
    def test_a_new_group_and_its_members_move_the_counters(self, pg: ModuleType, run: Run) -> None:
        """A delta, not an absolute: the whole point of `reach()` is that it
        counts every group in the deployment, including whatever else the suite
        has created."""
        before = run(platform_analytics.reach())

        extra = World(run)
        extra.setup()
        try:
            extra.add_user(admin=True)
            extra.add_user()
            after = run(platform_analytics.reach())
        finally:
            extra.teardown()

        assert after.groups == before.groups + 1
        assert after.members == before.members + 2
        assert after.admins == before.admins + 1


class TestTopology:
    def test_the_fleet_queries_fan_out_and_that_is_the_deal(self, pg: ModuleType, run: Run) -> None:
        """The counterpart to `test_citus_topology.py`'s `Task Count: 1`.

        Every other query in this codebase is asserted to touch **one** shard.
        These do not, by design (see the module docstring), and the assertion
        that they aggregate *before* returning is what keeps the fan-out
        affordable: the plan's top node must be an aggregate over the custom
        scan, not a raw scan whose rows all cross the network.
        """
        plan = run(
            db.fetch(
                "EXPLAIN (COSTS OFF) "
                "SELECT day, sum(messages) FROM group_daily_stats "
                "WHERE day >= $1 AND day <= $2 GROUP BY day",
                DAY,
                NEXT,
                name="test_explain_platform_daily",
            )
        )
        text = "\n".join(row["QUERY PLAN"] for row in plan)

        assert "Aggregate" in text, text
        assert "Custom Scan (Citus Adaptive)" in text, text
