"""Unit coverage for the member registry — v1's `check_new_name`, as SQL.

`cb_core.members` is a thin repository, so what is worth asserting without a
database is the behaviour it adds *around* the SQL: the write-skip caches that
keep a reference-table write off every message, the COALESCE-shaped upsert v1's
"only overwrite with a non-null" rule needs, and the promise that a database
failure degrades to "not registered" instead of taking a reply down with it.

The SQL itself is exercised against real Citus in
`qa/integration/test_members.py`.
"""

from __future__ import annotations

from typing import Any

import pytest

from cb_core import members
from cb_core.members import MemberIdentity, MemberRef

ALICE = MemberIdentity(user_id=1, username="alice", first_name="Alice")
BOB = MemberIdentity(user_id=2, username="bob", first_name="Bob")
GROUP = -1001


class _RecordingDB:
    """Stands in for `cb_core.db` and remembers every statement it was asked to run."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fail = fail

    async def execute(self, stmt: str, *args: Any, name: str = "execute") -> str:
        self.calls.append((name, args))
        if self.fail:
            raise RuntimeError("connection refused")
        return "INSERT 0 1"

    async def fetch(self, stmt: str, *args: Any, name: str = "fetch") -> list[Any]:
        self.calls.append((name, args))
        if self.fail:
            raise RuntimeError("connection refused")
        return [{"user_id": 1, "username": "alice"}, {"user_id": 2, "username": "bob"}]

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


@pytest.fixture(autouse=True)
def _clean_caches() -> None:
    members.reset_cache()


@pytest.fixture
def fake_db(monkeypatch: pytest.MonkeyPatch) -> _RecordingDB:
    db = _RecordingDB()
    monkeypatch.setattr(members, "db", db)
    return db


# ------------------------------------------------------------------- recording


@pytest.mark.asyncio
async def test_first_message_writes_both_rows(fake_db: _RecordingDB) -> None:
    await members.record(GROUP, ALICE)
    assert fake_db.names() == ["members_upsert_user", "members_upsert_membership"]


@pytest.mark.asyncio
async def test_repeat_messages_write_nothing(fake_db: _RecordingDB) -> None:
    """`users` is a Citus reference table — every write replicates to every
    node. v1 skipped the same round trip via `cache_users` (UserRegisters.py:35)."""
    for _ in range(5):
        await members.record(GROUP, ALICE)
    assert fake_db.names() == ["members_upsert_user", "members_upsert_membership"]


@pytest.mark.asyncio
async def test_a_rename_writes_again(fake_db: _RecordingDB) -> None:
    await members.record(GROUP, ALICE)
    renamed = MemberIdentity(user_id=1, username="alice2", first_name="Alice")
    await members.record(GROUP, renamed)
    assert fake_db.names().count("members_upsert_user") == 2
    # ...but the membership row is already there and is not rewritten.
    assert fake_db.names().count("members_upsert_membership") == 1


@pytest.mark.asyncio
async def test_the_same_user_in_a_second_group_writes_a_membership(fake_db: _RecordingDB) -> None:
    await members.record(GROUP, ALICE)
    await members.record(-1002, ALICE)
    assert fake_db.names().count("members_upsert_user") == 1
    assert fake_db.names().count("members_upsert_membership") == 2


@pytest.mark.asyncio
async def test_a_dm_records_the_user_but_no_membership(fake_db: _RecordingDB) -> None:
    """v1's register half is explicitly `if chat_type in ['group','supergroup']`
    (UserRegisters.py:85); the `users` half runs everywhere."""
    await members.record(0, ALICE)
    assert fake_db.names() == ["members_upsert_user"]


@pytest.mark.asyncio
async def test_leaving_lets_the_next_message_re_register(fake_db: _RecordingDB) -> None:
    await members.record(GROUP, ALICE)
    await members.mark_left(GROUP, ALICE.user_id)
    await members.record(GROUP, ALICE)
    assert fake_db.names().count("members_upsert_membership") == 2


# ------------------------------------------------------------------- degrading


@pytest.mark.asyncio
async def test_a_database_failure_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bookkeeping on the reply path. v1's equivalent returned an empty register
    when its backend call failed (UserRegisters.py:18-19) — it never surfaced."""
    monkeypatch.setattr(members, "db", _RecordingDB(fail=True))
    await members.record(GROUP, ALICE)
    await members.mark_left(GROUP, ALICE.user_id)
    assert await members.random_usernames(GROUP, 2) == []


@pytest.mark.asyncio
async def test_a_failed_write_is_not_cached_as_done(monkeypatch: pytest.MonkeyPatch) -> None:
    failing = _RecordingDB(fail=True)
    monkeypatch.setattr(members, "db", failing)
    await members.record(GROUP, ALICE)

    working = _RecordingDB()
    monkeypatch.setattr(members, "db", working)
    await members.record(GROUP, ALICE)
    assert working.names() == ["members_upsert_user", "members_upsert_membership"]


# ---------------------------------------------------------------------- reads


@pytest.mark.asyncio
async def test_random_usernames_asks_for_exactly_what_it_needs(fake_db: _RecordingDB) -> None:
    assert await members.random_usernames(GROUP, 2) == ["alice", "bob"]
    assert fake_db.calls == [("members_random", (GROUP, 2))]


@pytest.mark.asyncio
async def test_random_usernames_of_zero_never_queries(fake_db: _RecordingDB) -> None:
    assert await members.random_usernames(GROUP, 0) == []
    assert fake_db.calls == []


@pytest.mark.asyncio
async def test_roster_is_one_statement_ordered_by_user_id(fake_db: _RecordingDB) -> None:
    """The whole point of the port (spec D-EV-1): one call, not one per member."""
    assert await members.roster(GROUP) == (
        MemberRef(user_id=1, username="alice"),
        MemberRef(user_id=2, username="bob"),
    )
    assert fake_db.calls == [("members_roster", (GROUP,))]


@pytest.mark.asyncio
async def test_roster_degrades_to_empty_on_a_database_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(members, "db", _RecordingDB(fail=True))
    assert await members.roster(GROUP) == ()


# -------------------------------------------------------------------- the SQL


def test_the_user_upsert_never_overwrites_a_known_value_with_null() -> None:
    """v1 only copied a field when the update carried a non-null for it
    (UserRegisters.py:57-59). COALESCE is that rule; EXCLUDED alone would erase
    a `last_name` every time Telegram omitted one."""
    for column in ("username", "first_name", "last_name", "language_code"):
        assert f"{column}" in members._UPSERT_USER  # noqa: SLF001
        assert f"COALESCE(EXCLUDED.{column}, users.{column})" in members._UPSERT_USER  # noqa: SLF001


def test_the_registry_never_writes_a_join_time() -> None:
    """The regression migration `0004` exists to prevent: hearing from someone
    is not watching them join. `core_mediarestrict` restricts media from anyone
    whose `joined_at` is inside the window, so a registry insert claiming
    `now()` would mute every long-standing member on their first message after a
    deploy. Only the join handler may write that column."""
    assert "joined_at" not in members._UPSERT_MEMBERSHIP  # noqa: SLF001


def test_every_query_filters_on_the_distribution_column() -> None:
    """AGENTS.md §4.1 — a query without `group_id` fans out to every shard."""
    for stmt in (
        members._UPSERT_MEMBERSHIP,  # noqa: SLF001
        members._MARK_LEFT,  # noqa: SLF001
        members._RANDOM_USERNAMES,  # noqa: SLF001
        members._COUNT_MEMBERS,  # noqa: SLF001
        members._ROSTER,  # noqa: SLF001
    ):
        assert "group_id" in stmt
