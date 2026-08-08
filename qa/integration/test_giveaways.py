"""`cb_core.giveaways` against a real Citus — the two things v1's SQLite
could not do.

v1 kept the entrants as a comma-joined string it read, edited in Python and
wrote back whole (`../COOKIEBOT-Telegram-Group-Bot/Bot/Giveaways.py:81-94`),
under a process-wide `RLock` that only bound one process. This layer proves
the replacement: the entry is one statement, a second press by the same person
is a primary-key conflict rather than a substring scan, two presses that
interleave both survive, and every statement is a single-shard router query.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

import pytest

from cb_core import db, giveaways
from qa.integration.factories import World

pytestmark = pytest.mark.integration

Run = Callable[[Coroutine[Any, Any, Any]], Any]


def _create(world: World, run: Run, *, winners: int = 1, message_id: int = 9001) -> Any:
    return run(
        giveaways.create(
            group_id=world.group_id,
            message_id=message_id,
            creator_id=world.add_user(admin=True).user_id,
            prize="Fursuit of Mekhy 🐾🦝",
            winners_wanted=winners,
        )
    )


def test_a_giveaway_is_found_by_the_message_its_buttons_are_on(world: World, run: Run) -> None:
    giveaway_id = _create(world, run, winners=3)
    found = run(giveaways.by_message(world.group_id, 9001))
    assert found is not None
    assert found.giveaway_id == giveaway_id
    assert found.winners_wanted == 3
    # The full prize survives the round trip — v1 stored the 20-character
    # truncation of a `json.dumps` instead (D-GA-1).
    assert found.prize == "Fursuit of Mekhy 🐾🦝"


def test_another_groups_giveaway_is_not_visible(world: World, run: Run) -> None:
    _create(world, run)
    # v1's lookup was `WHERE message_id = ?` with no chat predicate at all, so
    # two groups whose announcements shared a message id answered each other's
    # presses.
    assert run(giveaways.by_message(world.group_id - 7, 9001)) is None


def test_entering_twice_is_reported_as_already_in(world: World, run: Run) -> None:
    giveaway_id = _create(world, run)
    member = world.add_user()
    assert run(
        giveaways.enter(world.group_id, giveaway_id, user_id=member.user_id, display_name="@one")
    )
    assert not run(
        giveaways.enter(world.group_id, giveaway_id, user_id=member.user_id, display_name="@one")
    )
    assert len(run(giveaways.participants(world.group_id, giveaway_id))) == 1


def test_two_members_sharing_a_first_name_are_two_entrants(world: World, run: Run) -> None:
    """v1 identified an entrant by the display name it had joined into a
    string, so two members called "Alex" with no username were one entrant."""
    giveaway_id = _create(world, run)
    a, b = world.add_user(), world.add_user()
    run(giveaways.enter(world.group_id, giveaway_id, user_id=a.user_id, display_name="Alex"))
    run(giveaways.enter(world.group_id, giveaway_id, user_id=b.user_id, display_name="Alex"))
    assert len(run(giveaways.participants(world.group_id, giveaway_id))) == 2


def test_concurrent_entries_do_not_overwrite_each_other(world: World, run: Run) -> None:
    """The lost update v1's read-modify-write string had. Ten presses land at
    once; all ten must be there afterwards."""
    giveaway_id = _create(world, run)
    members = world.add_users(10)

    async def press_all() -> None:
        await asyncio.gather(
            *(
                giveaways.enter(
                    world.group_id,
                    giveaway_id,
                    user_id=member.user_id,
                    display_name=f"@{member.username}",
                )
                for member in members
            )
        )

    run(press_all())
    assert len(run(giveaways.participants(world.group_id, giveaway_id))) == 10


def test_deleting_the_giveaway_takes_its_entrants_with_it(world: World, run: Run) -> None:
    giveaway_id = _create(world, run)
    member = world.add_user()
    run(giveaways.enter(world.group_id, giveaway_id, user_id=member.user_id, display_name="@x"))
    run(giveaways.delete(world.group_id, giveaway_id))
    assert run(giveaways.by_message(world.group_id, 9001)) is None
    assert run(giveaways.participants(world.group_id, giveaway_id)) == ()


def test_repoint_moves_the_raffle_to_the_draw_more_message(world: World, run: Run) -> None:
    giveaway_id = _create(world, run)
    run(giveaways.repoint(world.group_id, giveaway_id, 9002))
    assert run(giveaways.by_message(world.group_id, 9001)) is None
    found = run(giveaways.by_message(world.group_id, 9002))
    assert found is not None and found.giveaway_id == giveaway_id


def test_every_read_is_a_single_shard_router_query(world: World, run: Run) -> None:
    """AGENTS.md §4.6: verify, don't assume. Both tables are distributed on
    `group_id` and every statement carries it, so Citus must plan one task."""
    giveaway_id = _create(world, run)
    plans = {
        "by_message": (
            "SELECT group_id FROM giveaways WHERE group_id = $1 AND message_id = $2",
            (world.group_id, 9001),
        ),
        "participants": (
            "SELECT user_id FROM giveaway_participants WHERE group_id = $1 AND giveaway_id = $2",
            (world.group_id, giveaway_id),
        ),
    }
    for label, (stmt, args) in plans.items():
        rows = run(db.fetch(f"EXPLAIN {stmt}", *args, name=f"explain_{label}"))
        plan = "\n".join(row[0] for row in rows)
        assert "Task Count: 1" in plan, f"{label} fans out:\n{plan}"
