"""Tests for `persistence.py` and its wiring into `SandboxStore`.

`tmp_path` gives every test its own DuckDB file — DuckDB's single-writer rule
means two `SandboxStore`s can never hold the same file read-write at once, so
a test that opens a second one on the same path always `close()`s the first.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from cb_sandbox.persistence import SandboxDB
from cb_sandbox.state import (
    Membership,
    SandboxChat,
    SandboxMessage,
    SandboxScenario,
    SandboxStore,
    SandboxUser,
)

#: Deliberately the exact values a fresh `SandboxStore`'s own counters start
#: from — the round-trip and counter tests both lean on that to prove a
#: restored id is treated as already spent, not just present in memory.
_USER_ID = 500_000_001
_CHAT_ID = -1_001_000_000_001
_MESSAGE_ID = 1000
#: The exact id a fresh `SandboxStore`'s own `next_scenario_id` mints first —
#: same convention as the three ids just above.
_SCENARIO_ID = "scenario-1"


def _seed_one_of_everything(store: SandboxStore) -> None:
    """One user, one chat, one membership, one scenario, one message, one API
    call — enough to touch every table `persistence.py` owns. The scenario is
    activated before the message/API call are recorded so both come out
    tagged, exercising the stamping path alongside the round trip."""
    user = SandboxUser(id=_USER_ID, first_name="Alice", username="alice", last_name="Zed")
    store.users[user.id] = user
    chat = SandboxChat(id=_CHAT_ID, title="Sandbox Group")
    store.chats[chat.id] = chat
    chat.members[user.id] = Membership(user_id=user.id, role="creator", anonymous=False)
    scenario = SandboxScenario(
        id=store.next_scenario_id(),
        name="core_rules pt",
        description="checks /regras answers in Portuguese",
        source="e2e",
        tags=["captcha"],
        metadata={"nodeid": "qa/test_core_rules.py::test_pt"},
        status="passed",
        notes=[{"at": 1_700_000_000.0, "text": "issued", "level": "info"}],
    )
    store.scenarios[scenario.id] = scenario
    store.active_scenario_id = scenario.id
    message = SandboxMessage(
        message_id=_MESSAGE_ID,
        chat_id=chat.id,
        from_id=user.id,
        text="hello",
        date=1_700_000_000.0,
    )
    store.add_message(message)
    store.record_api_call("sendMessage", {"chat_id": chat.id, "text": "hello"})


class TestRoundTrip:
    def test_fresh_store_sees_what_the_previous_one_saved(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "sandbox.duckdb")
        first = SandboxStore(db_path)
        _seed_one_of_everything(first)
        first.close()

        second = SandboxStore(db_path)
        try:
            assert set(second.users) == {_USER_ID}
            user = second.users[_USER_ID]
            assert (user.first_name, user.username, user.last_name) == ("Alice", "alice", "Zed")

            assert set(second.chats) == {_CHAT_ID}
            chat = second.chats[_CHAT_ID]
            assert (chat.title, chat.type) == ("Sandbox Group", "supergroup")

            membership = chat.members[_USER_ID]
            assert (membership.role, membership.anonymous) == ("creator", False)

            messages = second.messages[_CHAT_ID]
            assert len(messages) == 1
            assert messages[0].text == "hello"
            # Stamped at record time from `active_scenario_id` — the tag itself
            # is what a restart must not lose, not just the scenario row.
            assert messages[0].scenario_id == _SCENARIO_ID

            assert len(second.api_calls) == 1
            assert second.api_calls[0]["method"] == "sendMessage"
            assert second.api_calls[0]["scenario_id"] == _SCENARIO_ID

            assert set(second.scenarios) == {_SCENARIO_ID}
            scenario = second.scenarios[_SCENARIO_ID]
            assert scenario.name == "core_rules pt"
            assert scenario.description == "checks /regras answers in Portuguese"
            assert scenario.source == "e2e"
            assert scenario.tags == ["captcha"]
            assert scenario.metadata == {"nodeid": "qa/test_core_rules.py::test_pt"}
            assert scenario.status == "passed"
            assert scenario.notes == [{"at": 1_700_000_000.0, "text": "issued", "level": "info"}]
            assert scenario.ended_at is None

            # `active_scenario_id` is session state, not world state — like
            # `pending_updates`, it does not survive a restart.
            assert second.active_scenario_id is None
        finally:
            second.close()

    def test_counters_resume_past_restored_ids(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "sandbox.duckdb")
        first = SandboxStore(db_path)
        _seed_one_of_everything(first)
        first.close()

        second = SandboxStore(db_path)
        try:
            assert second.next_user_id() > _USER_ID
            assert second.next_chat_id() < _CHAT_ID
            assert second.next_message_id() > _MESSAGE_ID
            # Otherwise a caller minting an id with no `id` of their own would
            # immediately collide with the one just restored from disk, and
            # every retry after that would collide again — see
            # `SandboxStore._resync_counters`.
            assert second.next_scenario_id() != _SCENARIO_ID
        finally:
            second.close()


class TestIdempotentSave:
    @pytest.mark.parametrize("kind", ["user", "chat", "member", "message"])
    def test_resaving_the_same_row_does_not_duplicate_it(self, tmp_path: Path, kind: str) -> None:
        db = SandboxDB(str(tmp_path / "sandbox.duckdb"))
        user = SandboxUser(id=1, first_name="Bob", username="bob")
        chat = SandboxChat(id=-1, title="Group")
        membership = Membership(user_id=user.id, role="member")
        message = SandboxMessage(
            message_id=1000, chat_id=chat.id, from_id=user.id, text="hi", date=1.0
        )

        if kind == "user":
            db.save_user(user)
            db.save_user(user)
        elif kind == "chat":
            db.save_chat(chat)
            db.save_chat(chat)
        elif kind == "member":
            db.save_chat(chat)
            db.save_member(chat.id, membership)
            db.save_member(chat.id, membership)
        else:
            db.save_chat(chat)
            db.save_message(message)
            db.save_message(message)

        sink = SandboxStore(str(tmp_path / "sink.duckdb"))
        try:
            db.load_into(sink)
            if kind == "user":
                assert len(sink.users) == 1
            elif kind == "chat":
                assert len(sink.chats) == 1
            elif kind == "member":
                assert len(sink.chats[chat.id].members) == 1
            else:
                assert len(sink.messages[chat.id]) == 1
        finally:
            sink.close()
            db.close()


def test_reset_clears_memory_and_the_file(tmp_path: Path) -> None:
    db_path = str(tmp_path / "sandbox.duckdb")
    store = SandboxStore(db_path)
    _seed_one_of_everything(store)
    assert store.users and store.chats and store.messages and store.api_calls
    assert store.scenarios and store.active_scenario_id is not None

    store.reset()
    assert store.users == {}
    assert store.chats == {}
    assert store.messages == {}
    assert store.api_calls == []
    assert store.scenarios == {}
    assert store.active_scenario_id is None
    store.close()

    reopened = SandboxStore(db_path)
    try:
        assert reopened.users == {}
        assert reopened.chats == {}
        assert reopened.messages == {}
        assert reopened.api_calls == []
        assert reopened.scenarios == {}
    finally:
        reopened.close()


class TestCounterSurvivesClear:
    """The regression for the confirmed bug: `SandboxStore.reset()` used to
    restart `update_id`/`message_id` at their base values, which collided
    with ids cb-gateway's Valkey dedupe middleware already had recorded as
    delivered (see `SandboxStore.next_update_id`'s docstring and
    `docs/SANDBOX.md`'s "Bot API compatibility" section). `db.clear()` is the
    exact call `reset()` makes; `sandbox_counters` is the one table it must
    not touch.
    """

    def test_clear_does_not_delete_the_counters_table(self, tmp_path: Path) -> None:
        db = SandboxDB(str(tmp_path / "sandbox.duckdb"))
        db.save_counter("update_id_high_water", 42)
        db.save_counter("message_id_high_water", 4242)

        db.clear()

        assert db.load_counters() == {"update_id_high_water": 42, "message_id_high_water": 4242}
        db.close()

    def test_store_reset_resumes_ids_past_the_pre_reset_high_water_mark(
        self, tmp_path: Path
    ) -> None:
        store = SandboxStore(str(tmp_path / "sandbox.duckdb"))
        try:
            for _ in range(5):
                store.next_update_id()
            highest_before_reset = store.next_update_id()

            store.reset()

            assert store.next_update_id() > highest_before_reset
        finally:
            store.close()


def test_read_only_open_of_a_missing_file_degrades_to_empty(tmp_path: Path) -> None:
    missing_path = str(tmp_path / "never-written.duckdb")
    sink = SandboxStore(str(tmp_path / "sink.duckdb"))
    try:
        db = SandboxDB(missing_path, read_only=True)
        db.load_into(sink)  # must not raise
        assert sink.users == {}
        assert sink.chats == {}
        assert sink.messages == {}
    finally:
        sink.close()


class _ExplodingConnection:
    """Stands in for a DuckDB connection whose disk write fails — a plain
    Python fake, not a monkeypatched C extension method, so this works
    regardless of what the real `duckdb.DuckDBPyConnection` allows overriding."""

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("disk is full")


def test_write_failure_leaves_the_in_memory_sandbox_working(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SandboxStore(str(tmp_path / "sandbox.duckdb"))
    try:
        # `_conn`: the only way to simulate the disk failing underneath an
        # already-open connection without depending on duckdb's own
        # C-extension attribute rules.
        monkeypatch.setattr(store.db, "_conn", _ExplodingConnection())

        user = SandboxUser(id=999, first_name="Carol", username="carol")
        store.users[user.id] = user  # must not raise despite every write failing
        assert store.users[999].first_name == "Carol"

        chat = SandboxChat(id=-999, title="Broken Group")
        store.chats[chat.id] = chat
        chat.members[user.id] = Membership(user_id=user.id, role="member")
        assert chat.members[user.id].role == "member"

        message = SandboxMessage(
            message_id=1000, chat_id=chat.id, from_id=user.id, text="still here", date=1.0
        )
        store.add_message(message)
        assert store.messages[chat.id][0].text == "still here"

        store.record_api_call("sendMessage", {"chat_id": chat.id})
        assert store.api_calls[-1]["method"] == "sendMessage"

        scenario = SandboxScenario(id="scenario-broken", name="broken")
        store.scenarios[scenario.id] = scenario  # must not raise either
        assert store.scenarios["scenario-broken"].name == "broken"

        store.publish("member", {"chat_id": chat.id, "user_id": user.id})
    finally:
        store.close()
