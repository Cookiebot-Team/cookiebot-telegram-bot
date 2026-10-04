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
from datetime import UTC, datetime, timedelta
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

        (event,) = run(audit.page(world.group_id, limit=10)).events
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
        (event,) = run(audit.page(world.group_id, limit=10)).events
        assert event.actor_user_id is None


class TestPaging:
    def test_pages_are_newest_first_and_lose_nothing(self, run: Run, world: World) -> None:
        _seed(run, world.group_id, 7)

        first = run(audit.page(world.group_id, limit=3))
        second = run(audit.page(world.group_id, limit=3, before_id=first.next_before))
        third = run(audit.page(world.group_id, limit=3, before_id=second.next_before))

        assert [len(first.events), len(second.events), len(third.events)] == [3, 3, 1]
        assert third.next_before is None
        ids = [event.id for event in (*first.events, *second.events, *third.events)]
        assert len(set(ids)) == 7  # every row, exactly once
        assert ids == sorted(ids, reverse=True)  # UUIDv7 sorts by creation time

    def test_a_filter_narrows_without_breaking_the_cursor(self, run: Run, world: World) -> None:
        _seed(run, world.group_id, 2, action=audit.CONFIG_UPDATED)
        _seed(run, world.group_id, 2, action=audit.RULES_UPDATED)

        rules = run(audit.page(world.group_id, limit=10, action=audit.RULES_UPDATED)).events
        assert len(rules) == 2
        assert {event.action for event in rules} == {audit.RULES_UPDATED}

    def test_an_actor_filter_is_a_single_shard_read(self, run: Run, world: World) -> None:
        run(audit.record(world.group_id, audit.CONFIG_UPDATED, actor_user_id=OTHER_ACTOR))
        _seed(run, world.group_id, 2)

        mine = run(audit.page(world.group_id, limit=10, actor_user_id=OTHER_ACTOR)).events
        assert [event.actor_user_id for event in mine] == [OTHER_ACTOR]

    def test_next_before_is_null_on_an_exact_limit_last_page(self, run: Run, world: World) -> None:
        """D1: exactly `limit` rows left is the last page, not a page and an empty one."""
        _seed(run, world.group_id, 4)
        first = run(audit.page(world.group_id, limit=2))
        assert first.next_before == first.events[-1].id
        last = run(audit.page(world.group_id, limit=2, before_id=first.next_before))
        assert len(last.events) == 2
        assert last.next_before is None
        assert run(audit.page(world.group_id, limit=4)).next_before is None


class TestFilters:
    def _mixed(self, run: Run, world: World) -> None:
        for surface, action in (
            ("telegram", audit.CONFIG_UPDATED),
            ("miniapp", audit.CONFIG_UPDATED),
            ("miniapp", audit.RULES_UPDATED),
            ("api", audit.RULES_UPDATED),
        ):
            run(
                audit.record(
                    world.group_id,
                    action,
                    actor_user_id=OTHER_ACTOR if surface == "api" else ACTOR,
                    surface=surface,
                )
            )

    def test_surface_alone(self, run: Run, world: World) -> None:
        self._mixed(run, world)
        got = run(audit.page(world.group_id, surface="miniapp")).events
        assert [e.surface for e in got] == ["miniapp", "miniapp"]

    def test_window_alone_is_inclusive_since_exclusive_until(self, run: Run, world: World) -> None:
        base = datetime(2026, 1, 1, 12, tzinfo=UTC)
        for hour in range(3):
            run(
                audit.record(world.group_id, audit.CONFIG_UPDATED, now=base + timedelta(hours=hour))
            )
        got = run(audit.page(world.group_id, since=base, until=base + timedelta(hours=2))).events
        assert [e.ts for e in got] == [base + timedelta(hours=1), base]
        assert len(run(audit.page(world.group_id, since=base + timedelta(hours=2))).events) == 1
        assert len(run(audit.page(world.group_id, until=base)).events) == 0

    def test_naive_datetimes_are_utc(self, run: Run, world: World) -> None:
        stamp = datetime(2026, 2, 1, 8, tzinfo=UTC)
        run(audit.record(world.group_id, audit.CONFIG_UPDATED, now=stamp))
        naive = datetime(2026, 2, 1, 8)
        assert len(run(audit.page(world.group_id, since=naive)).events) == 1
        assert len(run(audit.page(world.group_id, until=naive)).events) == 0

    def test_all_filters_combined(self, run: Run, world: World) -> None:
        self._mixed(run, world)
        got = run(
            audit.page(
                world.group_id,
                action=audit.RULES_UPDATED,
                actor_user_id=ACTOR,
                surface="miniapp",
                since=datetime.now(UTC) - timedelta(minutes=5),
                until=datetime.now(UTC) + timedelta(minutes=5),
            )
        ).events
        assert len(got) == 1
        assert (got[0].action, got[0].surface, got[0].actor_user_id) == (
            audit.RULES_UPDATED,
            "miniapp",
            ACTOR,
        )


class TestFleet:
    def test_reads_across_groups_and_pages_without_gaps(self, run: Run, world: World) -> None:
        other = World(run)
        other.setup()
        try:
            started = datetime.now(UTC) - timedelta(seconds=1)
            for index in range(5):
                run(
                    audit.record(
                        world.group_id if index % 2 else other.group_id, audit.CONFIG_UPDATED
                    )
                )
            seen: list[Any] = []
            cursor = None
            while True:
                got = run(
                    audit.fleet_page(tenant_id="cookiebot", limit=2, before=cursor, since=started)
                )
                seen.extend(got.events)
                cursor = got.next_before
                if cursor is None:
                    break
            mine = [e for e in seen if e.event.group_id in (world.group_id, other.group_id)]
            ids = [e.event.id for e in seen]
            assert len(mine) == 5
            assert len(set(ids)) == len(ids)
            assert ids == sorted(ids, reverse=True)
            assert {e.event.group_id for e in mine} == {world.group_id, other.group_id}
            assert {e.group_title for e in mine} == {
                f"QA Group {world.group_id}",
                f"QA Group {other.group_id}",
            }
        finally:
            run(_purge(other.group_id))
            other.teardown()

    def test_another_tenants_rows_never_appear(self, run: Run, world: World) -> None:
        foreign = World(run)
        foreign.setup()
        try:
            started = datetime.now(UTC) - timedelta(seconds=1)
            run(_ensure_tenant("bombot"))
            run(_set_tenant(foreign.group_id, "bombot"))
            _seed(run, foreign.group_id, 2)
            _seed(run, world.group_id, 1)
            got = run(audit.fleet_page(tenant_id="cookiebot", limit=100, since=started)).events
            assert foreign.group_id not in {e.event.group_id for e in got}
            assert world.group_id in {e.event.group_id for e in got}
            theirs = run(audit.fleet_page(tenant_id="bombot", limit=100, group_id=foreign.group_id))
            assert len(theirs.events) == 2
            wrong = run(audit.fleet_page(tenant_id="cookiebot", group_id=foreign.group_id))
            assert wrong.events == ()
        finally:
            run(_purge(foreign.group_id))
            foreign.teardown()

    def test_filters_apply_to_the_fleet_read(self, run: Run, world: World) -> None:
        run(
            audit.record(
                world.group_id, audit.RULES_UPDATED, surface="api", actor_user_id=OTHER_ACTOR
            )
        )
        _seed(run, world.group_id, 2)
        got = run(
            audit.fleet_page(
                tenant_id="cookiebot",
                group_id=world.group_id,
                action=audit.RULES_UPDATED,
                surface="api",
                actor_user_id=OTHER_ACTOR,
            )
        ).events
        assert len(got) == 1

    def test_with_a_group_the_read_is_single_shard(self, run: Run, world: World) -> None:
        args = ("cookiebot", world.group_id, None, None, None, None, None, None, 51)
        plan = run(_explain(audit.fleet_sql(world.group_id), *args))
        if "Task Count" not in plan:
            pytest.skip("not a distributed plan — citus is not installed here")
        assert "Task Count: 1" in plan, plan


class TestIsolation:
    def test_one_groups_trail_never_shows_anothers(self, run: Run, world: World) -> None:
        other = World(run)
        other.setup()
        try:
            _seed(run, world.group_id, 2)
            _seed(run, other.group_id, 3)
            assert len(run(audit.page(world.group_id, limit=50)).events) == 2
            assert len(run(audit.page(other.group_id, limit=50)).events) == 3
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
        assert run(audit.page(doomed.group_id, limit=10)).events == ()


class TestTopology:
    def test_the_page_query_hits_one_shard(self, run: Run, world: World) -> None:
        """`Task Count: 1` — the whole reason `group_id` is the shard key and
        leads the primary key (AGENTS.md §4). On plain Postgres the plan has no
        task count at all, and the assertion is skipped rather than faked."""
        stmt = audit._PAGE  # noqa: SLF001
        plan = run(_explain(stmt, world.group_id, None, None, None, None, None, None, 51))
        if "Task Count" not in plan:
            pytest.skip("not a distributed plan — citus is not installed here")
        assert "Task Count: 1" in plan, plan


async def _purge(group_id: int) -> None:
    from cb_core import db

    await db.execute(
        "DELETE FROM group_audit_events WHERE group_id = $1", group_id, name="test_cleanup"
    )


async def _ensure_tenant(tenant_id: str) -> None:
    from cb_core import db

    await db.execute(
        "INSERT INTO tenants (tenant_id, display_name) VALUES ($1, $1) ON CONFLICT DO NOTHING",
        tenant_id,
        name="test_ensure_tenant",
    )


async def _set_tenant(group_id: int, tenant_id: str) -> None:
    from cb_core import db

    await db.execute(
        "UPDATE groups SET tenant_id = $2 WHERE group_id = $1",
        group_id,
        tenant_id,
        name="test_set_tenant",
    )


async def _explain(stmt: str, *args: Any) -> str:
    from cb_core import db

    rows = await db.fetch(f"EXPLAIN (COSTS OFF) {stmt}", *args, name="test_explain")
    return "\n".join(str(record[0]) for record in rows)
