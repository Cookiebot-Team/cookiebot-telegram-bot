"""`group_members.joined_at` against a real Citus database — the DB seam
`cb_gateway.handlers.mediarestrict` owns (`_record_join` / `_joined_at`).

Mirrors the pattern in `qa/integration/test_group_welcomes.py` (the sibling
`core_welcome` port, same "real DB seam + Citus topology" shape) and the
topology checks in `qa/integration/test_citus_topology.py`. `group_members`
itself is already asserted distributed-on-`group_id` and colocated with
`groups` there; this file adds the one query shape this feature actually
depends on at message time.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import Any

import pytest

from cb_gateway.handlers import mediarestrict
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


class TestRecordJoin:
    def test_first_join_creates_the_row(self, run: Run, world: World, pg: ModuleType) -> None:
        user_id = 900_000_001

        run(mediarestrict._record_join(world.group_id, user_id))  # noqa: SLF001

        row = run(
            pg.fetchrow(
                "SELECT joined_at, left_at FROM group_members WHERE group_id = $1 AND user_id = $2",
                world.group_id,
                user_id,
                name="test_read_member",
            )
        )
        assert row is not None
        assert row["left_at"] is None

    def test_a_second_join_does_not_move_the_clock(
        self, run: Run, world: World, pg: ModuleType
    ) -> None:
        """`ON CONFLICT (group_id, user_id) DO NOTHING` (mediarestrict.py) — a
        rejoin (or a duplicated join event) must not reset an already-recorded
        `joined_at`."""
        user_id = 900_000_002
        run(mediarestrict._record_join(world.group_id, user_id))  # noqa: SLF001
        # Computed in Python and bound as a plain value, not `joined_at - interval
        # '1 hour'` in SQL: Citus rejects a STABLE expression (timestamptz
        # subtraction depends on the session TimeZone) that references a column
        # of a distributed table in an UPDATE's SET clause — the same
        # restriction `cb_core.group_config.set_config` already documents for
        # `now()` in a `DO UPDATE SET`.
        backdated = datetime.now(UTC) - timedelta(hours=1)
        run(
            pg.execute(
                "UPDATE group_members SET joined_at = $3 WHERE group_id = $1 AND user_id = $2",
                world.group_id,
                user_id,
                backdated,
                name="test_backdate_join",
            )
        )

        run(mediarestrict._record_join(world.group_id, user_id))  # noqa: SLF001

        assert run(mediarestrict._joined_at(world.group_id, user_id)) == backdated  # noqa: SLF001

    def test_row_is_deleted_when_the_group_is_deleted(
        self, run: Run, world: World, pg: ModuleType
    ) -> None:
        """`group_members.group_id` FK is `ON DELETE CASCADE` (migration 0001)."""
        user_id = 900_000_003
        run(mediarestrict._record_join(world.group_id, user_id))  # noqa: SLF001

        run(
            pg.execute(
                "DELETE FROM groups WHERE group_id = $1", world.group_id, name="test_drop_group"
            )
        )

        row = run(
            pg.fetchrow(
                "SELECT 1 FROM group_members WHERE group_id = $1 AND user_id = $2",
                world.group_id,
                user_id,
                name="test_member_row_gone",
            )
        )
        assert row is None


class TestJoinedAt:
    def test_no_row_at_all_reads_as_none(self, run: Run, world: World) -> None:
        assert run(mediarestrict._joined_at(world.group_id, 900_000_099)) is None  # noqa: SLF001

    def test_ignores_a_member_who_left(self, run: Run, world: World, pg: ModuleType) -> None:
        """The predicate matches `group_members_joined_idx`'s own filter
        (`WHERE left_at IS NULL`, migration 0001) — a member who left must not
        be treated as "still within the restriction window" just because
        their old `joined_at` happens to be recent."""
        user_id = 900_000_004
        run(mediarestrict._record_join(world.group_id, user_id))  # noqa: SLF001
        run(
            pg.execute(
                "UPDATE group_members SET left_at = now() WHERE group_id = $1 AND user_id = $2",
                world.group_id,
                user_id,
                name="test_mark_left",
            )
        )

        assert run(mediarestrict._joined_at(world.group_id, user_id)) is None  # noqa: SLF001


class TestCitusTopology:
    """The read this feature depends on at message time must touch exactly one
    shard — AGENTS.md §4 rule 6, same guard `test_group_welcomes.py` and
    `test_citus_topology.py` apply to their own hot queries."""

    def _task_count(
        self,
        citus: ModuleType,
        run: Run,
        sql: str,
        *args: int,
    ) -> int:
        plan = run(citus.fetch(f"EXPLAIN (COSTS OFF) {sql}", *args))
        for row in plan:
            line = str(row[0]).strip()
            if line.startswith("Task Count:"):
                return int(line.split(":")[1])
        raise AssertionError("no Task Count in plan:\n" + "\n".join(str(r[0]) for r in plan))

    def test_joined_at_lookup_is_single_shard(
        self, citus: ModuleType, run: Run, world: World
    ) -> None:
        n = self._task_count(
            citus,
            run,
            "SELECT joined_at FROM group_members "
            "WHERE group_id = $1 AND user_id = $2 AND left_at IS NULL",
            world.group_id,
            900_000_001,
        )
        assert n == 1

    def test_record_join_upsert_is_single_shard(
        self, citus: ModuleType, run: Run, world: World
    ) -> None:
        n = self._task_count(
            citus,
            run,
            "INSERT INTO group_members (group_id, user_id) VALUES ($1, $2) "
            "ON CONFLICT (group_id, user_id) DO NOTHING",
            world.group_id,
            900_000_005,
        )
        assert n == 1
