"""`group_rules` against a real Citus database.

Exercises the DB seam `cb_gateway.handlers.rules` owns (`_fetch_rules` /
`_upsert_rules`) against the real `group_rules` table
(`packages/cb-api/migrations/versions/0001_initial_schema.py`), plus the Citus
single-shard guarantee the read/write depend on: `group_rules` is distributed
on `group_id`, colocated with `groups` (AGENTS.md §4). Mirrors the pattern in
`qa/integration/test_group_config.py` and the topology checks in
`qa/integration/test_citus_topology.py`.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from types import ModuleType
from typing import Any

import pytest

from cb_gateway.handlers import rules
from qa.integration.factories import World

pytestmark = pytest.mark.integration

Run = Callable[[Coroutine[Any, Any, Any]], Any]


@pytest.fixture(scope="module")
def citus(pg: ModuleType, run: Run) -> ModuleType:
    """Same guard as qa/integration/test_citus_topology.py: skip on plain Postgres."""
    row = run(pg.fetchrow("SELECT count(*) AS n FROM pg_extension WHERE extname = 'citus'"))
    if not row or row["n"] == 0:
        pytest.skip("citus extension not installed")
    return pg


class TestFetch:
    def test_a_group_with_no_row_has_no_rules(self, run: Run, world: World) -> None:
        assert run(rules._fetch_rules(world.group_id)) is None  # noqa: SLF001

    def test_reads_the_row_back(self, run: Run, world: World, pg: ModuleType) -> None:
        run(
            pg.execute(
                "INSERT INTO group_rules (group_id, body, updated_by) VALUES ($1, $2, $3)",
                world.group_id,
                "Be nice to each other.",
                world.add_user(admin=True).user_id,
                name="test_seed_rules",
            )
        )

        assert run(rules._fetch_rules(world.group_id)) == "Be nice to each other."  # noqa: SLF001


class TestUpsert:
    def test_first_write_creates_the_row(self, run: Run, world: World, pg: ModuleType) -> None:
        run(rules._upsert_rules(world.group_id, world.add_user(admin=True).user_id, "No spam."))  # noqa: SLF001

        row = run(
            pg.fetchrow(
                "SELECT body, updated_by FROM group_rules WHERE group_id = $1",
                world.group_id,
                name="test_read_rules_row",
            )
        )
        assert row is not None
        assert row["body"] == "No spam."

    def test_second_write_updates_the_same_row(
        self, run: Run, world: World, pg: ModuleType
    ) -> None:
        admin = world.add_user(admin=True)
        run(rules._upsert_rules(world.group_id, admin.user_id, "First version."))  # noqa: SLF001
        run(rules._upsert_rules(world.group_id, admin.user_id, "Second version."))  # noqa: SLF001

        rows = run(
            pg.fetch(
                "SELECT body FROM group_rules WHERE group_id = $1",
                world.group_id,
                name="test_count_rules_rows",
            )
        )
        assert len(rows) == 1
        assert rows[0]["body"] == "Second version."

    def test_updated_by_can_be_null_for_an_anonymous_admin(
        self, run: Run, world: World, pg: ModuleType
    ) -> None:
        """An anonymous admin has no resolvable user id (docs/contracts/admins.md);
        `updated_by` must accept NULL rather than fail the write."""
        run(rules._upsert_rules(world.group_id, None, "Rules set anonymously."))  # noqa: SLF001

        row = run(
            pg.fetchrow(
                "SELECT body, updated_by FROM group_rules WHERE group_id = $1",
                world.group_id,
                name="test_read_anonymous_rules_row",
            )
        )
        assert row is not None
        assert row["updated_by"] is None

    def test_row_is_deleted_when_the_group_is_deleted(
        self, run: Run, world: World, pg: ModuleType
    ) -> None:
        """`group_rules.group_id` FK is `ON DELETE CASCADE` (migration 0001)."""
        run(rules._upsert_rules(world.group_id, None, "Some rules."))  # noqa: SLF001

        run(
            pg.execute(
                "DELETE FROM groups WHERE group_id = $1", world.group_id, name="test_drop_group"
            )
        )

        row = run(
            pg.fetchrow(
                "SELECT 1 FROM group_rules WHERE group_id = $1",
                world.group_id,
                name="test_rules_row_gone",
            )
        )
        assert row is None


class TestCitusTopology:
    """The read/write queries must touch exactly one shard — AGENTS.md §4 rule 6."""

    def test_rules_lookup_is_single_shard(self, citus: ModuleType, run: Run, world: World) -> None:
        plan = run(
            citus.fetch(
                "EXPLAIN (COSTS OFF) SELECT body FROM group_rules WHERE group_id = $1",
                world.group_id,
            )
        )
        task_count = None
        for row in plan:
            line = str(row[0]).strip()
            if line.startswith("Task Count:"):
                task_count = int(line.split(":")[1])
                break
        assert task_count is not None, "no Task Count in plan:\n" + "\n".join(
            str(r[0]) for r in plan
        )
        assert task_count == 1
