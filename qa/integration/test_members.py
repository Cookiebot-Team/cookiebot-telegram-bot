"""The member registry against a real Citus — the rows v1 kept in Mongo.

Simulates what v1's `check_new_name` did on every message
(`../COOKIEBOT-Telegram-Group-Bot/Bot/UserRegisters.py:64-88`) and asserts the
rows that come out, including the two places this port deliberately behaves
better than v1: a rename keeps one row instead of creating a stranger, and
leaving stamps `left_at` instead of deleting the membership `joined_at` lives on.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from types import ModuleType
from typing import TYPE_CHECKING, Any

import pytest

from cb_core import db, members
from cb_core.members import MemberIdentity

if TYPE_CHECKING:
    from qa.integration.factories import World

pytestmark = pytest.mark.integration

Run = Callable[[Coroutine[Any, Any, Any]], Any]


@pytest.fixture(autouse=True)
def _clean_caches(pg: ModuleType) -> None:
    """The write-skip caches are process-global; a test that expects a write to
    reach the database has to start from an empty one."""
    members.reset_cache()


def _identity(user_id: int, **fields: Any) -> MemberIdentity:
    return MemberIdentity(user_id=user_id, **fields)


def _cleanup_user(run: Run, user_id: int) -> None:
    run(db.execute("DELETE FROM users WHERE user_id = $1", user_id, name="test_cleanup"))


class TestRecord:
    def test_a_first_message_registers_the_user_and_the_membership(
        self, run: Run, world: World
    ) -> None:
        uid = 700_000_001
        run(members.record(world.group_id, _identity(uid, username="speaker", first_name="Spk")))

        user = run(db.fetchrow("SELECT * FROM users WHERE user_id = $1", uid))
        assert user is not None
        assert user["username"] == "speaker"
        assert run(members.count(world.group_id)) == 1
        _cleanup_user(run, uid)

    def test_a_rename_updates_the_same_row(self, run: Run, world: World) -> None:
        """v1's register held usernames, so a rename made the member vanish and
        re-register as someone new. Here the key is the user id."""
        uid = 700_000_002
        run(members.record(world.group_id, _identity(uid, username="before", first_name="N")))
        members.reset_cache()
        run(members.record(world.group_id, _identity(uid, username="after", first_name="N")))

        rows = run(db.fetch("SELECT username FROM users WHERE user_id = $1", uid))
        assert [r["username"] for r in rows] == ["after"]
        assert run(members.count(world.group_id)) == 1
        _cleanup_user(run, uid)

    def test_an_update_without_a_last_name_does_not_erase_one(self, run: Run, world: World) -> None:
        """v1 only copied non-null fields (`UserRegisters.py:57-59`); Telegram
        omits `last_name` for users who have none set *and* in some payloads."""
        uid = 700_000_003
        run(
            members.record(
                world.group_id, _identity(uid, username="w", first_name="W", last_name="Wolf")
            )
        )
        members.reset_cache()
        run(members.record(world.group_id, _identity(uid, username="w", first_name="W")))

        row = run(db.fetchrow("SELECT last_name FROM users WHERE user_id = $1", uid))
        assert row is not None
        assert row["last_name"] == "Wolf"
        _cleanup_user(run, uid)


class TestJoinTime:
    def test_the_registry_leaves_joined_at_unknown(self, run: Run, world: World) -> None:
        """Migration 0004's whole reason. A member the bot merely *heard from*
        has no known join time, and `core_mediarestrict` fails open on NULL —
        which is what keeps a five-year member from being muted on their first
        message after a deploy."""
        uid = 700_000_007
        run(members.record(world.group_id, _identity(uid, username="veteran", first_name="V")))
        row = run(
            db.fetchrow(
                "SELECT joined_at, first_seen_at FROM group_members "
                "WHERE group_id = $1 AND user_id = $2",
                world.group_id,
                uid,
            )
        )
        assert row is not None
        assert row["joined_at"] is None
        assert row["first_seen_at"] is not None
        _cleanup_user(run, uid)

    def test_a_witnessed_join_fills_in_the_unknown_time(self, run: Run, world: World) -> None:
        """The registry may have created the row first (they spoke before the
        join event was processed, or before the bot restarted). The join event
        is the one thing that can answer `joined_at`, so it fills the gap
        instead of doing nothing."""
        from cb_gateway.handlers import mediarestrict

        uid = 700_000_008
        run(members.record(world.group_id, _identity(uid, username="joiner", first_name="J")))
        run(mediarestrict._record_join(world.group_id, uid))  # noqa: SLF001

        assert run(mediarestrict._joined_at(world.group_id, uid)) is not None  # noqa: SLF001
        _cleanup_user(run, uid)

    def test_a_second_join_does_not_move_a_known_time(self, run: Run, world: World) -> None:
        from cb_gateway.handlers import mediarestrict

        uid = 700_000_009
        run(mediarestrict._record_join(world.group_id, uid))  # noqa: SLF001
        first = run(mediarestrict._joined_at(world.group_id, uid))  # noqa: SLF001
        run(mediarestrict._record_join(world.group_id, uid))  # noqa: SLF001

        assert run(mediarestrict._joined_at(world.group_id, uid)) == first  # noqa: SLF001


class TestLeaving:
    def test_leaving_stamps_left_at_and_drops_out_of_the_count(
        self, run: Run, world: World
    ) -> None:
        uid = 700_000_004
        run(members.record(world.group_id, _identity(uid, username="gone", first_name="G")))
        run(members.mark_left(world.group_id, uid))

        row = run(
            db.fetchrow(
                "SELECT joined_at, left_at FROM group_members WHERE group_id = $1 AND user_id = $2",
                world.group_id,
                uid,
            )
        )
        assert row is not None, "the membership row must survive — joined_at is load-bearing"
        assert row["left_at"] is not None
        assert run(members.count(world.group_id)) == 0
        _cleanup_user(run, uid)

    def test_rejoining_clears_left_at_without_moving_joined_at(
        self, run: Run, world: World
    ) -> None:
        """`core_mediarestrict` measures a member's age from `joined_at`. If a
        rejoin reset it, a five-year member who left for a minute would come back
        newly restricted."""
        uid = 700_000_005
        run(members.record(world.group_id, _identity(uid, username="back", first_name="B")))
        original = run(
            db.fetchrow(
                "SELECT joined_at FROM group_members WHERE group_id = $1 AND user_id = $2",
                world.group_id,
                uid,
            )
        )
        run(members.mark_left(world.group_id, uid))
        run(members.record(world.group_id, _identity(uid, username="back", first_name="B")))

        row = run(
            db.fetchrow(
                "SELECT joined_at, left_at FROM group_members WHERE group_id = $1 AND user_id = $2",
                world.group_id,
                uid,
            )
        )
        assert row is not None
        assert row["left_at"] is None
        assert original is not None
        assert row["joined_at"] == original["joined_at"]
        _cleanup_user(run, uid)


class TestRandomUsernames:
    def test_returns_two_distinct_registered_members(self, run: Run, world: World) -> None:
        world.add_users(4)
        picked = run(members.random_usernames(world.group_id, 2))
        assert len(picked) == 2
        assert len(set(picked)) == 2
        assert set(picked) <= {u.username for u in world.users}

    def test_returns_fewer_than_asked_when_the_group_is_too_small(
        self, run: Run, world: World
    ) -> None:
        """This is `fun_ship`'s `no_ship` path — v1's `except IndexError`."""
        world.add_user()
        assert len(run(members.random_usernames(world.group_id, 2))) == 1

    def test_never_returns_a_member_of_another_group(self, run: Run, world: World) -> None:
        """The whole point of the distribution column: one shard, one group."""
        world.add_users(3)
        other = run(members.random_usernames(world.group_id - 7, 2))
        assert other == []

    def test_a_member_who_left_is_not_shippable(self, run: Run, world: World) -> None:
        alice, bob = world.add_users(2)
        run(members.mark_left(world.group_id, bob.user_id))
        picked = run(members.random_usernames(world.group_id, 2))
        assert picked == [alice.username]

    def test_a_member_with_no_username_is_skipped(self, run: Run, world: World) -> None:
        """v1's register only ever held usernames — `if username and ...`
        (`UserRegisters.py:84`). A user who has never set one cannot be
        @-mentioned, so shipping them would produce dead text."""
        named = world.add_user()
        uid = 700_000_006
        run(members.record(world.group_id, _identity(uid, first_name="Nameless")))
        picked = run(members.random_usernames(world.group_id, 5))
        assert picked == [named.username]
        _cleanup_user(run, uid)
