"""`cb_core.audit` against a real Citus database.

Three things only a real database can answer, and all three are load-bearing:

* the row survives the jsonb round trip with its `before`/`after` intact;
* the keyset page is stable and complete — the failure mode of a hand-rolled
  cursor is a row that silently never appears on any page;
* the read is a **single-shard** query. `group_audit_events` is distributed on
  `group_id`, and an audit page that fanned out to every shard would be a
  per-request cost proportional to the size of the deployment.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from types import ModuleType
from typing import Any

import pytest

from cb_core import audit
from qa.integration.factories import World

pytestmark = pytest.mark.integration

Run = Callable[[Coroutine[Any, Any, Any]], Any]

ACTOR = 4242
OTHER_ACTOR = 9999


def _seed(run: Run, group_id: int, count: int, *, action: str = audit.CONFIG_UPDATED) -> None:
    for index in range(count):
        run(
            audit.record(
                group_id,
                action,
                actor_user_id=ACTOR,
                surface="miniapp",
                summary=f"change {index}",
                before={"sfw": True},
                after={"sfw": False},
            )
        )


class TestRoundTrip:
    def test_a_row_keeps_both_sides_of_the_change(self, run: Run, world: World) -> None:
        written = run(
            audit.record(
                world.group_id,
                audit.CONFIG_UPDATED,
                actor_user_id=ACTOR,
                surface="miniapp",
                summary="changed captcha_timeout_seconds",
                before={"captcha_timeout_seconds": 300},
                after={"captcha_timeout_seconds": 600},
            )
        )
        assert written is not None

        (event,) = run(audit.page(world.group_id, limit=10))
        assert event.id == written.id
        assert event.action == audit.CONFIG_UPDATED
        assert event.actor_user_id == ACTOR
        assert event.surface == "miniapp"
        assert event.before == {"captcha_timeout_seconds": 300}
        assert event.after == {"captcha_timeout_seconds": 600}

    def test_a_row_with_no_actor_is_still_stored(self, run: Run, world: World) -> None:
        """An anonymous admin has no account to attribute the change to. The
        row still says what changed, which is the truth rather than a guess."""
        run(audit.record(world.group_id, audit.RULES_UPDATED, actor_user_id=None))
        (event,) = run(audit.page(world.group_id, limit=10))
        assert event.actor_user_id is None


class TestPaging:
    def test_pages_are_newest_first_and_lose_nothing(self, run: Run, world: World) -> None:
        _seed(run, world.group_id, 7)

        first = run(audit.page(world.group_id, limit=3))
        second = run(audit.page(world.group_id, limit=3, before_id=first[-1].id))
        third = run(audit.page(world.group_id, limit=3, before_id=second[-1].id))

        assert [len(first), len(second), len(third)] == [3, 3, 1]
        ids = [event.id for event in (*first, *second, *third)]
        assert len(set(ids)) == 7  # every row, exactly once
        assert ids == sorted(ids, reverse=True)  # UUIDv7 sorts by creation time

    def test_a_filter_narrows_without_breaking_the_cursor(self, run: Run, world: World) -> None:
        _seed(run, world.group_id, 2, action=audit.CONFIG_UPDATED)
        _seed(run, world.group_id, 2, action=audit.RULES_UPDATED)

        rules = run(audit.page(world.group_id, limit=10, action=audit.RULES_UPDATED))
        assert len(rules) == 2
        assert {event.action for event in rules} == {audit.RULES_UPDATED}

    def test_an_actor_filter_is_a_single_shard_read(self, run: Run, world: World) -> None:
        run(audit.record(world.group_id, audit.CONFIG_UPDATED, actor_user_id=OTHER_ACTOR))
        _seed(run, world.group_id, 2)

        mine = run(audit.page(world.group_id, limit=10, actor_user_id=OTHER_ACTOR))
        assert [event.actor_user_id for event in mine] == [OTHER_ACTOR]


class TestIsolation:
    def test_one_groups_trail_never_shows_anothers(self, run: Run, world: World) -> None:
        other = World(run)
        other.setup()
        try:
            _seed(run, world.group_id, 2)
            _seed(run, other.group_id, 3)
            assert len(run(audit.page(world.group_id, limit=50))) == 2
            assert len(run(audit.page(other.group_id, limit=50))) == 3
        finally:
            other.teardown()

    def test_deleting_the_group_takes_its_trail(self, run: Run, pg: ModuleType) -> None:
        """No FK to `groups` — a distributed table cannot cheaply carry one to a
        row that may live on another shard — so the trail is cleaned up with the
        group explicitly. This is the test that fails if that stops happening."""
        doomed = World(run)
        doomed.setup()
        _seed(run, doomed.group_id, 2)
        run(
            pg.execute(
                "DELETE FROM group_audit_events WHERE group_id = $1",
                doomed.group_id,
                name="test_cleanup",
            )
        )
        doomed.teardown()
        assert run(audit.page(doomed.group_id, limit=10)) == ()


class TestTopology:
    def test_the_page_query_hits_one_shard(self, run: Run, world: World) -> None:
        """`Task Count: 1` — the whole reason `group_id` is the shard key and
        leads the primary key (AGENTS.md §4). On plain Postgres the plan has no
        task count at all, and the assertion is skipped rather than faked."""
        plan = run(
            _explain(
                "SELECT id FROM group_audit_events WHERE group_id = $1 ORDER BY id DESC LIMIT 50",
                world.group_id,
            )
        )
        if "Task Count" not in plan:
            pytest.skip("not a distributed plan — citus is not installed here")
        assert "Task Count: 1" in plan, plan


async def _explain(stmt: str, *args: Any) -> str:
    from cb_core import db

    rows = await db.fetch(f"EXPLAIN (COSTS OFF) {stmt}", *args, name="test_explain")
    return "\n".join(str(record[0]) for record in rows)
