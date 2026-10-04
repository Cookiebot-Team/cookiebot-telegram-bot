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

from collections.abc import Callable, Coroutine, Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from cb_core import analytics, db
from qa.integration.factories import World

pytestmark = pytest.mark.integration

Run = Callable[[Coroutine[Any, Any, Any]], Any]

DAY = date(2026, 2, 10)
TODAY = datetime.now(UTC).date()
YESTERDAY = TODAY - timedelta(days=1)
_START = datetime(TODAY.year, TODAY.month, TODAY.day, tzinfo=UTC)
_END = _START + timedelta(days=1)


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
            (analytics._LIVE_DAILY, (_START, _END)),  # noqa: SLF001 - EXPLAIN the exact SQL the module runs
            (analytics._LIVE_COMMANDS, (_START, _END)),  # noqa: SLF001 - EXPLAIN the exact SQL the module runs
            (analytics._LIVE_LLM, (_START, _END)),  # noqa: SLF001 - EXPLAIN the exact SQL the module runs
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


# ----------------------------------------------------------------- live today
#
# `x_live_stats` R2: today's numbers come from the raw events, the rollup
# covers only days before today.


@pytest.fixture
def live_group(run: Run, world: World) -> Iterator[World]:
    """`world`, plus a clean slate for the raw/rollup tables the group-delete
    cascade does not reach (`message_events` is partitioned and has no FK)."""
    run(db.fetchrow("SELECT cb_maintain_partitions(7, 7)", name="test_partitions"))
    # `cb_maintain_partitions` only creates today onward; yesterday's events need
    # a partition too (a long-lived database has it, a freshly migrated one may not).
    run(
        db.execute(
            """
            DO $$
            DECLARE d date := current_date - 1;
                    part text := format('message_events_p%s', to_char(d, 'YYYY_MM_DD'));
            BEGIN
                IF to_regclass(part) IS NULL THEN
                    EXECUTE format(
                        'CREATE TABLE %I PARTITION OF message_events FOR VALUES FROM (%L) TO (%L)',
                        part, d, d + 1);
                END IF;
            END $$
            """,
            name="test_yesterday_partition",
        )
    )
    yield world
    for table in ("message_events", "group_daily_stats", "command_daily_stats", "llm_daily_cost"):
        run(
            db.execute(
                f"DELETE FROM {table} WHERE group_id = $1",
                world.group_id,
                name="test_live_cleanup",
            )
        )


def _at(day: date, seconds: int = 60) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=UTC) + timedelta(seconds=seconds)


def _event(
    run: Run,
    group_id: int,
    day: date,
    event_type: str,
    *,
    user_id: int | None = 1,
    command: str | None = None,
    outcome: str = "ok",
    latency_ms: int | None = None,
    llm_tokens: int | None = None,
    llm_cost_usd: float | None = None,
    seconds: int = 60,
) -> None:
    run(
        db.execute(
            "INSERT INTO message_events (ts, group_id, user_id, event_type, command, outcome,"
            " latency_ms, llm_tokens, llm_cost_usd) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)",
            _at(day, seconds),
            group_id,
            user_id,
            event_type,
            command,
            outcome,
            latency_ms,
            llm_tokens,
            llm_cost_usd,
            name="test_seed_event",
        )
    )


def _usage(
    run: Run,
    group_id: int,
    day: date,
    provider: str,
    model: str,
    *,
    cost_usd: float,
    outcome: str = "ok",
) -> None:
    run(
        db.execute(
            "INSERT INTO llm_usage (group_id, usage_id, task, provider, model, input_tokens,"
            " output_tokens, cost_usd, outcome, created_at)"
            " VALUES ($1, cb_uuid_v7(), 'chat', $2, $3, 10, 5, $4, $5, $6)",
            group_id,
            provider,
            model,
            cost_usd,
            outcome,
            _at(day),
            name="test_seed_usage",
        )
    )


def _seed_today(run: Run, group_id: int, day: date) -> None:
    _event(run, group_id, day, "message", user_id=1, llm_tokens=100, llm_cost_usd=0.01)
    _event(run, group_id, day, "message", user_id=2)
    _event(run, group_id, day, "command", user_id=1, command="meme", latency_ms=50)
    _event(
        run, group_id, day, "command", user_id=3, command="meme", outcome="error", latency_ms=400
    )
    _event(run, group_id, day, "command", user_id=3, command="battle", latency_ms=10)
    _event(run, group_id, day, "join", user_id=4)
    _event(run, group_id, day, "leave", user_id=5)
    _event(run, group_id, day, "captcha", user_id=4, outcome="issued")
    _event(run, group_id, day, "captcha", user_id=4, outcome="solved")
    _usage(run, group_id, day, "anthropic", "claude-sonnet-5", cost_usd=0.5)
    _usage(run, group_id, day, "anthropic", "claude-sonnet-5", cost_usd=0.25, outcome="error")
    _usage(run, group_id, day, "openai", "gpt-4o", cost_usd=0.1, outcome="refusal")


class TestLiveToday:
    def test_today_is_returned_with_no_rollup(self, run: Run, live_group: World) -> None:
        _seed_today(run, live_group.group_id, TODAY)

        rows = run(analytics.daily(live_group.group_id, YESTERDAY, TODAY))

        assert len(rows) == 1
        row = rows[0]
        assert row.day == TODAY
        assert (row.messages, row.commands, row.joins, row.leaves) == (2, 3, 1, 1)
        assert (row.captcha_issued, row.captcha_solved) == (1, 1)
        assert row.active_users == 5
        assert row.errors == 1
        assert row.p95_latency_ms == 400
        assert (row.llm_tokens, row.llm_cost_usd) == (100, 0.01)

        cmds = run(analytics.commands(live_group.group_id, YESTERDAY, TODAY))
        assert [(c.command, c.invocations, c.errors, c.p95_latency_ms) for c in cmds] == [
            ("meme", 2, 1, 400),
            ("battle", 1, 0, 10),
        ]

        costs = run(analytics.llm_costs(live_group.group_id, YESTERDAY, TODAY))
        assert [(c.model, c.calls, c.cost_usd, c.refusals, c.errors) for c in costs] == [
            ("claude-sonnet-5", 2, 0.75, 0, 1),
            ("gpt-4o", 1, 0.1, 1, 0),
        ]

    def test_a_window_that_excludes_today_ignores_live_events(
        self, run: Run, live_group: World
    ) -> None:
        _seed_today(run, live_group.group_id, TODAY)

        assert run(analytics.daily(live_group.group_id, DAY, DAY)) == ()
        assert run(analytics.commands(live_group.group_id, DAY, DAY)) == ()
        assert run(analytics.llm_costs(live_group.group_id, DAY, DAY)) == ()

    def test_a_quiet_today_adds_no_row(self, run: Run, live_group: World) -> None:
        assert run(analytics.daily(live_group.group_id, TODAY, TODAY)) == ()

    def test_yesterday_rolled_up_plus_today_raw_is_not_double_counted(
        self, run: Run, live_group: World
    ) -> None:
        gid = live_group.group_id
        _seed_today(run, gid, YESTERDAY)
        _seed_today(run, gid, TODAY)
        for fn in ("cb_rollup_day", "cb_rollup_llm_day"):
            run(db.execute(f"SELECT {fn}($1)", YESTERDAY, name="test_rollup"))
        # A rollup row for *today* (the 1-minute worker) must be ignored, not added.
        run(db.execute("SELECT cb_rollup_day($1)", TODAY, name="test_rollup"))
        run(db.execute("SELECT cb_rollup_llm_day($1)", TODAY, name="test_rollup"))
        _event(run, gid, TODAY, "message", user_id=9)  # arrived after the rollup

        rows = run(analytics.daily(gid, YESTERDAY, TODAY))
        assert [(r.day, r.messages) for r in rows] == [(YESTERDAY, 2), (TODAY, 3)]

        cmds = {c.command: c for c in run(analytics.commands(gid, YESTERDAY, TODAY))}
        assert (cmds["meme"].invocations, cmds["meme"].errors) == (4, 2)
        assert cmds["battle"].invocations == 2

        costs = {c.model: c for c in run(analytics.llm_costs(gid, YESTERDAY, TODAY))}
        assert costs["claude-sonnet-5"].calls == 4
        assert costs["claude-sonnet-5"].cost_usd == 1.5
        assert costs["gpt-4o"].calls == 2

    def test_live_and_rolled_up_numbers_are_identical(self, run: Run, live_group: World) -> None:
        gid = live_group.group_id
        _seed_today(run, gid, TODAY)

        live = (
            run(analytics.daily(gid, TODAY, TODAY)),
            run(analytics.commands(gid, TODAY, TODAY)),
            run(analytics.llm_costs(gid, TODAY, TODAY)),
        )
        run(db.execute("SELECT cb_rollup_day($1)", TODAY, name="test_rollup"))
        run(db.execute("SELECT cb_rollup_llm_day($1)", TODAY, name="test_rollup"))
        tomorrow = TODAY + timedelta(days=1)
        # Read the rollup back by pinning "today" to tomorrow.
        rolled = (
            run(analytics.daily(gid, TODAY, TODAY, today=tomorrow)),
            run(analytics.commands(gid, TODAY, TODAY, today=tomorrow)),
            run(analytics.llm_costs(gid, TODAY, TODAY, today=tomorrow)),
        )

        assert live[0] and live[1] and live[2]
        assert live == rolled

    def test_another_groups_events_are_never_counted(
        self, run: Run, live_group: World, second_world: World
    ) -> None:
        _event(run, live_group.group_id, TODAY, "message")
        _event(run, second_world.group_id, TODAY, "message")
        _event(run, second_world.group_id, TODAY, "message")
        try:
            rows = run(analytics.daily(live_group.group_id, TODAY, TODAY))
            assert [row.messages for row in rows] == [1]
        finally:
            run(
                db.execute(
                    "DELETE FROM message_events WHERE group_id = $1",
                    second_world.group_id,
                    name="test_live_cleanup",
                )
            )
