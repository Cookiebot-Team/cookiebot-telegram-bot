"""Unit coverage for `cb_core.birthdays` — `display_name`'s pure fallback and
`members_with_birthday`'s degrade-on-failure posture. The real single-shard
join is asserted against a real Citus in `qa/integration/test_birthdays.py`
(this module has no DB-free way to prove the SQL itself is correct); this
file mirrors `packages/cb-core/tests/test_members.py`'s `_RecordingDB` pattern
for the same reason that file gives.
"""

from __future__ import annotations

import random
from typing import Any

import pytest

from cb_core import birthdays
from cb_core.birthdays import BirthdayPerson


class _RecordingDB:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fail = fail

    async def fetch(self, stmt: str, *args: Any, name: str = "fetch") -> list[Any]:
        self.calls.append((name, args))
        if self.fail:
            raise RuntimeError("connection refused")
        return [
            {"user_id": 1, "username": "alice", "first_name": "Alice", "last_name": None},
            {"user_id": 2, "username": None, "first_name": "Bob", "last_name": "Smith"},
        ]


class TestDisplayName:
    def test_username_wins_when_present(self) -> None:
        person = BirthdayPerson(user_id=1, username="alice", first_name="Alice", last_name=None)
        assert birthdays.display_name(person) == "@alice"

    def test_falls_back_to_first_and_last_name(self) -> None:
        person = BirthdayPerson(user_id=2, username=None, first_name="Bob", last_name="Smith")
        assert birthdays.display_name(person) == "Bob Smith"

    def test_missing_last_name_does_not_leave_a_trailing_space(self) -> None:
        person = BirthdayPerson(user_id=3, username=None, first_name="Carol", last_name=None)
        assert birthdays.display_name(person) == "Carol"

    def test_missing_both_names_is_empty(self) -> None:
        person = BirthdayPerson(user_id=4, username=None, first_name=None, last_name=None)
        assert birthdays.display_name(person) == ""


class TestMembersWithBirthday:
    async def test_returns_the_mapped_rows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_db = _RecordingDB()
        monkeypatch.setattr(birthdays, "db", fake_db)
        people = await birthdays.members_with_birthday(555, 8, 2)
        assert people == (
            BirthdayPerson(user_id=1, username="alice", first_name="Alice", last_name=None),
            BirthdayPerson(user_id=2, username=None, first_name="Bob", last_name="Smith"),
        )
        assert fake_db.calls == [("birthdays_members", (555, 8, 2))]

    async def test_degrades_to_empty_on_a_database_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(birthdays, "db", _RecordingDB(fail=True))
        assert await birthdays.members_with_birthday(555, 8, 2) == ()


class TestBdayCatalog:
    def test_title_is_the_bare_command_prompt(self) -> None:
        assert "usernames" in birthdays.bday_title("en")

    def test_cta_substitutes_names(self) -> None:
        line = birthdays.bday_cta("en", names="Alice e Bob", rng=random.Random(1))
        assert "Alice e Bob" in line

    def test_cta_is_reproducible_with_a_seeded_rng(self) -> None:
        first = birthdays.bday_cta("en", names="Alice", rng=random.Random(7))
        second = birthdays.bday_cta("en", names="Alice", rng=random.Random(7))
        assert first == second

    def test_closing_substitutes_the_date(self) -> None:
        closing = birthdays.bday_closing("en", date="2026-08-02")
        assert "2026-08-02" in closing

    def test_next_header_is_localised_but_always_says_all_groups(self) -> None:
        # v1 does route this through i18n.get, unlike the "N dias:" per-day
        # line (which is a literal Python f-string, never i18n at all) --
        # each language has its own translated value, but all three still
        # say "(all groups)"/"(todos os grupos)", stale for this port's
        # single-group manual scope regardless of which one renders.
        assert birthdays.bday_next_header("en") == "UPCOMING BIRTHDAYS (all groups):\n\n"
        assert "todos" in birthdays.bday_next_header("pt")
