"""`cb_core.scheduled_posts` against a real Citus.

v1 kept these rows in a local SQLite file with no primary key, matched them by
parsing a formatted `name` string, and read the whole table into Python for
every question (`../COOKIEBOT-Telegram-Group-Bot/Bot/Publisher.py:15-17,101-127`).
Every predicate here is one of those Python scans, so the point of this suite is
that each one selects the same set — and that the ones which *can* be
single-shard are.

Contracts: `docs/contracts/util_postforwarder.md`,
`docs/contracts/util_deletereposts.md`.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import TYPE_CHECKING, Any

import pytest

from cb_core import scheduled_posts

if TYPE_CHECKING:
    from qa.integration.factories import World

pytestmark = pytest.mark.integration

Run = Callable[[Coroutine[Any, Any, Any]], Any]


def _create(run: Run, group_id: int, **overrides: Any) -> Any:
    kwargs: dict[str, Any] = {
        "group_id": group_id,
        "origin_title": "FurShop",
        "target_title": "Target Group",
        "days_remaining": 3,
        "next_run_at": datetime.now(UTC) + timedelta(days=1),
        "source_chat_id": -100777,
        "source_message_id": 42,
        "requester_chat_id": -100555,
        "requester_message_id": 7,
        "requester_user_id": 99,
    }
    kwargs.update(overrides)
    return run(scheduled_posts.create(**kwargs))


class TestRoundTrip:
    def test_a_created_row_comes_back_with_every_field(self, run: Run, world: World) -> None:
        post_id = _create(run, world.group_id, origin_title="RoundTrip Channel")
        found = run(scheduled_posts.find_by_origin_title("RoundTrip Channel"))
        assert found is not None
        assert found.post_id == post_id
        assert found.group_id == world.group_id
        assert found.source_message_id == 42
        assert found.requester_user_id == 99

    def test_the_key_is_a_uuid7(self, run: Run, world: World) -> None:
        """AGENTS.md §2.3 — app-generated, time-ordered, never uuid4."""
        from cb_core.ids import is_uuid7

        assert is_uuid7(_create(run, world.group_id))

    def test_count_for_group_is_scoped_to_the_group(self, run: Run, world: World) -> None:
        _create(run, world.group_id)
        _create(run, world.group_id)
        assert run(scheduled_posts.count_for_group(world.group_id)) == 2


class TestDueSweep:
    def test_only_rows_whose_time_has_passed_are_returned(self, run: Run, world: World) -> None:
        past = datetime.now(UTC) - timedelta(minutes=5)
        due_id = _create(run, world.group_id, next_run_at=past, origin_title="Due Channel")
        _create(run, world.group_id, origin_title="Future Channel")

        due = run(scheduled_posts.due_before(datetime.now(UTC)))
        ids = {p.post_id for p in due}
        assert due_id in ids
        assert all(p.next_run_at <= datetime.now(UTC) for p in due)

    def test_advance_spends_a_day_and_moves_the_clock(self, run: Run, world: World) -> None:
        """v1 `:337-339`."""
        post_id = _create(run, world.group_id, days_remaining=3, origin_title="Advance Channel")
        post = run(scheduled_posts.find_by_origin_title("Advance Channel"))
        assert post is not None

        later = datetime.now(UTC) + timedelta(days=1)
        assert run(scheduled_posts.advance_or_expire(post, later)) is True

        after = run(scheduled_posts.find_by_origin_title("Advance Channel"))
        assert after is not None
        assert after.post_id == post_id
        assert after.days_remaining == 2

    def test_the_last_day_deletes_the_row(self, run: Run, world: World) -> None:
        """v1 `:335-336`: `if job['days'] <= 1: delete_job(...)`."""
        _create(run, world.group_id, days_remaining=1, origin_title="Expiring Channel")
        post = run(scheduled_posts.find_by_origin_title("Expiring Channel"))
        assert post is not None

        assert run(scheduled_posts.advance_or_expire(post, datetime.now(UTC))) is False
        assert run(scheduled_posts.find_by_origin_title("Expiring Channel")) is None


class TestCampaignRules:
    def test_one_live_campaign_per_source_channel(self, run: Run, world: World) -> None:
        """v1 `:238-242` deletes every job whose name prefix matches the origin
        channel's title before scheduling a new run of it."""
        _create(run, world.group_id, origin_title="Repeat Channel")
        _create(run, world.group_id, origin_title="Other Channel")

        removed = run(scheduled_posts.delete_by_origin_title(world.group_id, "Repeat Channel"))
        assert removed == 1
        assert run(scheduled_posts.count_for_group(world.group_id)) == 1

    def test_trim_leaves_exactly_max_posts_minus_the_incoming_one(
        self, run: Run, world: World
    ) -> None:
        """D-PF-7, fixed. v1 mutated the list it was counting (`:261-267`), so
        the surviving number depended on iteration order; this evicts the oldest
        so that inserting one more lands exactly on the cap."""
        for i in range(5):
            _create(run, world.group_id, origin_title=f"Channel {i}")

        removed = run(scheduled_posts.trim_to_max(world.group_id, 3))
        assert removed == 3
        assert run(scheduled_posts.count_for_group(world.group_id)) == 2

    def test_the_default_cap_never_evicts_anything(self, run: Run, world: World) -> None:
        _create(run, world.group_id)
        assert run(scheduled_posts.trim_to_max(world.group_id, 9999)) == 0
        assert run(scheduled_posts.count_for_group(world.group_id)) == 1

    def test_trim_removes_the_oldest_first(self, run: Run, world: World) -> None:
        _create(run, world.group_id, origin_title="Oldest")
        _create(run, world.group_id, origin_title="Newest")

        run(scheduled_posts.trim_to_max(world.group_id, 2))
        assert run(scheduled_posts.find_by_origin_title("Oldest")) is None
        assert run(scheduled_posts.find_by_origin_title("Newest")) is not None


class TestCancel:
    """`util_deletereposts`. The rows a chat cancels are spread across every
    group its campaign targeted, so this predicate is deliberately not on the
    distribution column — see `cb_core/scheduled_posts.py`'s module docstring."""

    def test_every_group_the_requester_targeted_is_cancelled(
        self, run: Run, world: World, pg: ModuleType
    ) -> None:
        other_group = world.group_id - 1
        run(
            pg.execute(
                "INSERT INTO groups (group_id, title) VALUES ($1, 'Second Target')",
                other_group,
                name="test_second_group",
            )
        )
        try:
            _create(run, world.group_id, requester_chat_id=-100555)
            _create(run, other_group, requester_chat_id=-100555)
            _create(run, world.group_id, requester_chat_id=-100999, origin_title="Someone Else")

            removed = run(scheduled_posts.delete_by_requester(-100555))
            assert removed == 2
            assert run(scheduled_posts.count_for_group(other_group)) == 0
            assert run(scheduled_posts.count_for_group(world.group_id)) == 1
        finally:
            run(
                pg.execute(
                    "DELETE FROM groups WHERE group_id = $1", other_group, name="test_cleanup"
                )
            )

    def test_cancelling_a_chat_with_nothing_scheduled_is_a_no_op(self, run: Run) -> None:
        assert run(scheduled_posts.delete_by_requester(-100404)) == 0


class TestCitusShape:
    """AGENTS.md §4.6 — verify, don't assume."""

    def test_the_per_group_read_hits_one_shard(
        self, run: Run, world: World, pg: ModuleType
    ) -> None:
        _create(run, world.group_id)
        rows = run(
            pg.fetch(
                "EXPLAIN SELECT count(*) FROM scheduled_posts WHERE group_id = $1",
                world.group_id,
                name="explain_scheduled_posts",
            )
        )
        plan = "\n".join(r[0] for r in rows)
        assert "Task Count: 1" in plan, plan

    def test_scheduled_posts_is_colocated_with_groups(self, run: Run, pg: ModuleType) -> None:
        row = run(
            pg.fetchrow(
                """
                SELECT (SELECT colocationid FROM pg_dist_partition
                         WHERE logicalrelid = 'scheduled_posts'::regclass) AS posts,
                       (SELECT colocationid FROM pg_dist_partition
                         WHERE logicalrelid = 'groups'::regclass) AS groups
                """,
                name="colocation_check",
            )
        )
        assert row is not None
        assert row["posts"] == row["groups"]
