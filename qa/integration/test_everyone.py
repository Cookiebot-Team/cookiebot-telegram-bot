"""`members.roster()` against a real Citus — the batched read `util_everyone`
replaces v1's N+1 with (`UserRegisters.py:129`: one `GET users?username=` per
member). This is the query the port exists for, so its single-shard plan is
asserted here rather than deferred to the feature slice that calls it.

See `.specs/features/util_everyone/design.md` R1 and R6.3.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from types import ModuleType
from typing import TYPE_CHECKING, Any

import pytest

from cb_core import members
from cb_core.members import MemberRef

if TYPE_CHECKING:
    from qa.integration.factories import World

pytestmark = pytest.mark.integration

Run = Callable[[Coroutine[Any, Any, Any]], Any]


class TestRoster:
    def test_returns_seeded_members_ordered_by_user_id(self, run: Run, world: World) -> None:
        users = world.add_users(3)
        expected = tuple(
            sorted(
                (MemberRef(user_id=u.user_id, username=u.username) for u in users),
                key=lambda ref: ref.user_id,
            )
        )
        assert run(members.roster(world.group_id)) == expected

    def test_excludes_members_marked_left(self, run: Run, world: World) -> None:
        alice, bob = world.add_users(2)
        run(members.mark_left(world.group_id, bob.user_id))

        roster = run(members.roster(world.group_id))
        assert roster == (MemberRef(user_id=alice.user_id, username=alice.username),)

    def test_never_returns_a_member_of_another_group(self, run: Run, world: World) -> None:
        world.add_users(2)
        assert run(members.roster(world.group_id - 7)) == ()

    def test_an_empty_group_returns_an_empty_tuple(self, run: Run, world: World) -> None:
        assert run(members.roster(world.group_id)) == ()


@pytest.fixture(scope="module")
def citus(pg: ModuleType, run: Run) -> ModuleType:
    row = run(pg.fetchrow("SELECT count(*) AS n FROM pg_extension WHERE extname = 'citus'"))
    if not row or row["n"] == 0:
        pytest.skip("citus extension not installed")
    return pg


class TestRosterTopology:
    """AGENTS.md §4.6: verify, don't assume — `EXPLAIN` and check `Task Count: 1`."""

    def test_roster_query_is_single_shard(self, citus: ModuleType, run: Run, world: World) -> None:
        world.add_users(2)
        plan = run(
            citus.fetch(
                "EXPLAIN (COSTS OFF) "
                "SELECT m.user_id, u.username "
                "FROM group_members m "
                "JOIN users u ON u.user_id = m.user_id "
                "WHERE m.group_id = $1 AND m.left_at IS NULL "
                "ORDER BY m.user_id",
                world.group_id,
            )
        )
        for row in plan:
            line = str(row[0]).strip()
            if line.startswith("Task Count:"):
                assert int(line.split(":")[1]) == 1
                return
        raise AssertionError("no Task Count in plan:\n" + "\n".join(str(r[0]) for r in plan))
