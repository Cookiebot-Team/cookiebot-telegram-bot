"""Citus topology assertions.

Distribution and colocation are correctness properties here, not tuning. A table
that silently lands in the wrong colocation group turns every per-group join into
a repartition join, and nothing else in the test suite would notice.

These tests read `pg_dist_*` and `EXPLAIN` output, so they are the guard rail for
the rules in AGENTS.md §4. They skip on a plain Postgres without Citus.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from types import ModuleType
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from qa.integration.factories import World

pytestmark = pytest.mark.integration

DISTRIBUTED_TABLES = (
    "groups",
    "group_configs",
    "group_rules",
    "group_welcomes",
    "group_members",
    "group_admins",
    "captcha_challenges",
    "message_events",
    "group_daily_stats",
    "command_daily_stats",
    "media_objects",
    "llm_usage",
    "llm_daily_cost",
)

REFERENCE_TABLES = ("users", "blacklist", "bots", "command_catalog", "media_blobs")


@pytest.fixture(scope="module")
def citus(pg: ModuleType, run: Callable[[Coroutine[Any, Any, Any]], Any]) -> ModuleType:
    row = run(pg.fetchrow("SELECT count(*) AS n FROM pg_extension WHERE extname = 'citus'"))
    if not row or row["n"] == 0:
        pytest.skip("citus extension not installed")
    return pg


class TestDistribution:
    def test_tenant_tables_are_distributed_on_group_id(
        self, citus: ModuleType, run: Callable[[Coroutine[Any, Any, Any]], Any]
    ) -> None:
        rows = run(
            citus.fetch(
                """
                SELECT logicalrelid::regclass::text AS table_name,
                       column_to_column_name(logicalrelid, partkey) AS dist_column
                FROM pg_dist_partition
                WHERE partkey IS NOT NULL
                """
            )
        )
        actual = {r["table_name"]: r["dist_column"] for r in rows}
        for table in DISTRIBUTED_TABLES:
            assert actual.get(table) == "group_id", f"{table} is not distributed on group_id"

    def test_reference_tables_are_replicated(
        self, citus: ModuleType, run: Callable[[Coroutine[Any, Any, Any]], Any]
    ) -> None:
        rows = run(
            citus.fetch(
                """
                SELECT logicalrelid::regclass::text AS table_name
                FROM pg_dist_partition
                WHERE partkey IS NULL OR partkey = ''
                """
            )
        )
        actual = {r["table_name"] for r in rows}
        for table in REFERENCE_TABLES:
            assert table in actual, f"{table} is not a reference table"

    def test_everything_shares_one_colocation_group(
        self, citus: ModuleType, run: Callable[[Coroutine[Any, Any, Any]], Any]
    ) -> None:
        """Colocation is what makes a per-group join node-local."""
        rows = run(
            citus.fetch(
                """
                SELECT logicalrelid::regclass::text AS table_name, colocationid
                FROM pg_dist_partition
                WHERE partkey IS NOT NULL
                """
            )
        )
        colocation = {r["table_name"]: r["colocationid"] for r in rows}
        groups_colocation = colocation.get("groups")
        assert groups_colocation is not None
        for table in DISTRIBUTED_TABLES:
            assert colocation.get(table) == groups_colocation, (
                f"{table} is not colocated with groups — joins would repartition"
            )


class TestRouterQueries:
    """Queries on the reply path must touch exactly one shard."""

    def _task_count(
        self,
        citus: ModuleType,
        run: Callable[[Coroutine[Any, Any, Any]], Any],
        sql: str,
        *args: object,
    ) -> int:
        plan = run(citus.fetch(f"EXPLAIN (COSTS OFF) {sql}", *args))
        for row in plan:
            line = str(row[0]).strip()
            if line.startswith("Task Count:"):
                return int(line.split(":")[1])
        # A plan with no Task Count line never reached the Citus planner —
        # that means the table was not distributed at all.
        raise AssertionError("no Task Count in plan:\n" + "\n".join(str(r[0]) for r in plan))

    def test_config_lookup_is_single_shard(
        self, citus: ModuleType, run: Callable[[Coroutine[Any, Any, Any]], Any], world: World
    ) -> None:
        n = self._task_count(
            citus, run, "SELECT * FROM group_configs WHERE group_id = $1", world.group_id
        )
        assert n == 1

    def test_random_media_is_single_shard(
        self, citus: ModuleType, run: Callable[[Coroutine[Any, Any, Any]], Any], world: World
    ) -> None:
        n = self._task_count(
            citus,
            run,
            "SELECT * FROM media_objects WHERE group_id = $1 AND kind = ANY($2::text[]) "
            "ORDER BY random() LIMIT 1",
            world.group_id,
            ["photo"],
        )
        assert n == 1

    def test_member_join_to_reference_table_is_single_shard(
        self, citus: ModuleType, run: Callable[[Coroutine[Any, Any, Any]], Any], world: World
    ) -> None:
        """Distributed ⋈ reference stays local — no exchange."""
        n = self._task_count(
            citus,
            run,
            """
            SELECT u.username FROM group_members m
            JOIN users u ON u.user_id = m.user_id
            WHERE m.group_id = $1
            """,
            world.group_id,
        )
        assert n == 1

    def test_colocated_join_is_single_shard(
        self, citus: ModuleType, run: Callable[[Coroutine[Any, Any, Any]], Any], world: World
    ) -> None:
        n = self._task_count(
            citus,
            run,
            """
            SELECT c.language FROM group_configs c
            JOIN media_objects m ON m.group_id = c.group_id
            WHERE c.group_id = $1
            """,
            world.group_id,
        )
        assert n == 1

    def test_usage_lookup_is_single_shard(
        self, citus: ModuleType, run: Callable[[Coroutine[Any, Any, Any]], Any], world: World
    ) -> None:
        n = self._task_count(
            citus,
            run,
            "SELECT sum(cost_usd) FROM llm_usage WHERE group_id = $1",
            world.group_id,
        )
        assert n == 1


class TestConstraints:
    def test_unique_constraints_include_the_distribution_column(
        self, citus: ModuleType, run: Callable[[Coroutine[Any, Any, Any]], Any]
    ) -> None:
        """Citus requires it, and it also expresses the tenant-scoped intent."""
        rows = run(
            citus.fetch(
                """
                SELECT c.conrelid::regclass::text AS table_name, c.conname,
                       pg_get_constraintdef(c.oid) AS definition
                FROM pg_constraint c
                JOIN pg_dist_partition p ON p.logicalrelid = c.conrelid
                WHERE c.contype IN ('p', 'u') AND p.partkey IS NOT NULL
                """
            )
        )
        assert rows, "expected constraints on distributed tables"
        for row in rows:
            assert "group_id" in row["definition"], (
                f"{row['table_name']}.{row['conname']} omits group_id: {row['definition']}"
            )
