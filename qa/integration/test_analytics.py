"""`cb_core.analytics` against real rollup rows in real Citus.

The unit layer fakes these three queries; what it cannot check is that they
match the schema, that the aggregates are what Postgres actually computes, and
that each one is the single-shard router query AGENTS.md §4 requires — the
rollup tables are distributed on `group_id` and colocated with `groups`, so a
query that forgot the shard key would still return the right rows and quietly
fan out to every node.

Rows are written directly rather than through `cb_rollup_day`: the rollup
function is `qa/integration/test_rollups.py`'s subject, and what these need is
a known input, not a recomputed one.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from datetime import date, timedelta
from typing import Any

import pytest

from cb_core import analytics, db
from qa.integration.factories import World

pytestmark = pytest.mark.integration

Run = Callable[[Coroutine[Any, Any, Any]], Any]

DAY = date(2026, 2, 10)


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
            name="test_seed_daily",
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
            name="test_seed_command",
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
            name="test_seed_llm",
        )
    )


class TestDaily:
    def test_returns_the_window_in_order(self, run: Run, world: World) -> None:
        _seed_daily(run, world.group_id, DAY, messages=10, active_users=3)
        _seed_daily(run, world.group_id, DAY + timedelta(days=1), messages=4, active_users=2)

        rows = run(analytics.daily(world.group_id, DAY, DAY + timedelta(days=1)))

        assert [row.day for row in rows] == [DAY, DAY + timedelta(days=1)]
        assert [row.messages for row in rows] == [10, 4]

    def test_excludes_days_outside_the_window(self, run: Run, world: World) -> None:
        _seed_daily(run, world.group_id, DAY - timedelta(days=1), messages=99)
        _seed_daily(run, world.group_id, DAY, messages=10)

        rows = run(analytics.daily(world.group_id, DAY, DAY))

        assert [row.messages for row in rows] == [10]

    def test_another_groups_rows_are_never_returned(
        self, run: Run, world: World, second_world: World
    ) -> None:
        """The distribution column is also the authorisation boundary."""
        _seed_daily(run, world.group_id, DAY, messages=10)
        _seed_daily(run, second_world.group_id, DAY, messages=999)

        rows = run(analytics.daily(world.group_id, DAY, DAY))

        assert [row.messages for row in rows] == [10]

    def test_a_group_with_no_rows_is_empty_not_an_error(self, run: Run, world: World) -> None:
        assert run(analytics.daily(world.group_id, DAY, DAY)) == ()

    def test_numeric_cost_comes_back_as_a_float(self, run: Run, world: World) -> None:
        """`llm_cost_usd` is `numeric(12,4)`, which asyncpg hands back as
        `Decimal`; JSON has no Decimal, so the struct converts once here rather
        than at every call site."""
        _seed_daily(run, world.group_id, DAY, llm_cost_usd=1.25)

        rows = run(analytics.daily(world.group_id, DAY, DAY))

        assert isinstance(rows[0].llm_cost_usd, float)
        assert rows[0].llm_cost_usd == 1.25


class TestCommands:
    def test_totals_across_days_busiest_first(self, run: Run, world: World) -> None:
        _seed_command(run, world.group_id, DAY, "meme", invocations=5, errors=1)
        _seed_command(run, world.group_id, DAY + timedelta(days=1), "meme", invocations=7, errors=0)
        _seed_command(run, world.group_id, DAY, "battle", invocations=3, errors=0)

        rows = run(analytics.commands(world.group_id, DAY, DAY + timedelta(days=1)))

        assert [(row.command, row.invocations, row.errors) for row in rows] == [
            ("meme", 12, 1),
            ("battle", 3, 0),
        ]

    def test_p95_is_the_worst_day_not_an_average(self, run: Run, world: World) -> None:
        _seed_command(run, world.group_id, DAY, "meme", invocations=1, p95_latency_ms=100)
        _seed_command(
            run, world.group_id, DAY + timedelta(days=1), "meme", invocations=1, p95_latency_ms=900
        )

        rows = run(analytics.commands(world.group_id, DAY, DAY + timedelta(days=1)))

        assert rows[0].p95_latency_ms == 900

    def test_limit_is_applied(self, run: Run, world: World) -> None:
        for index, command in enumerate(("a", "b", "c")):
            _seed_command(run, world.group_id, DAY, command, invocations=10 - index)

        rows = run(analytics.commands(world.group_id, DAY, DAY, limit=2))

        assert [row.command for row in rows] == ["a", "b"]


class TestLlmCosts:
    def test_totals_per_provider_and_model_most_expensive_first(
        self, run: Run, world: World
    ) -> None:
        _seed_llm(run, world.group_id, DAY, "anthropic", "claude-sonnet-5", calls=2, cost_usd=0.5)
        _seed_llm(
            run,
            world.group_id,
            DAY + timedelta(days=1),
            "anthropic",
            "claude-sonnet-5",
            calls=3,
            cost_usd=0.25,
        )
        _seed_llm(run, world.group_id, DAY, "openai", "gpt-4o", calls=1, cost_usd=0.1)

        rows = run(analytics.llm_costs(world.group_id, DAY, DAY + timedelta(days=1)))

        assert [(row.model, row.calls, row.cost_usd) for row in rows] == [
            ("claude-sonnet-5", 5, 0.75),
            ("gpt-4o", 1, 0.1),
        ]


class TestCitusTopology:
    """AGENTS.md §4.6: verify, don't assume. Every one of these carries
    `group_id`, so the planner must route to exactly one shard."""

    @pytest.mark.parametrize(
        ("statement", "args"),
        [
            (
                "SELECT day FROM group_daily_stats WHERE group_id = $1 AND day >= $2 AND day <= $3",
                (DAY, DAY),
            ),
            (
                "SELECT command, sum(invocations) FROM command_daily_stats "
                "WHERE group_id = $1 AND day >= $2 AND day <= $3 GROUP BY command",
                (DAY, DAY),
            ),
            (
                "SELECT provider, sum(cost_usd) FROM llm_daily_cost "
                "WHERE group_id = $1 AND day >= $2 AND day <= $3 GROUP BY provider",
                (DAY, DAY),
            ),
        ],
    )
    def test_one_shard_per_query(
        self, run: Run, world: World, statement: str, args: tuple[Any, ...]
    ) -> None:
        plan = run(
            db.fetch(
                f"EXPLAIN (COSTS OFF) {statement}",
                world.group_id,
                *args,
                name="test_analytics_explain",
            )
        )
        text = "\n".join(str(row[0]) for row in plan)
        assert "Task Count: 1" in text, text
