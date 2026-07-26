"""Tests for the scenario concept: `POST /scenarios` and friends in
`control_api.py`, and the tagging `SandboxStore.add_message`/`record_api_call`
do in `state.py`.

The property under test throughout is the one the whole feature exists for:
after a long sandbox run, every message and every Bot API call can be sliced
back apart by which scenario was open when it happened — see
the package README and `SandboxScenario`'s docstring in `state.py`.

Builds a bare FastAPI app around just `control_api.router`, the same
isolation `test_control_api.py` uses — `telegram_api.py` is another agent's
concern. Where a test needs an API call recorded (not just a message), it
calls `store().record_api_call` directly rather than mounting the Bot API
surface, since tagging itself lives entirely in `state.py`.
"""

from __future__ import annotations

from typing import Any

import pytest
from cb_sandbox.control_api import router
from cb_sandbox.state import store
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def client() -> TestClient:
    store().reset()
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _seed(client: TestClient, scenario: str = "default") -> dict[str, Any]:
    resp = client.post("/api/seed", json={"scenario": scenario})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _user(snapshot: dict[str, Any], username: str) -> dict[str, Any]:
    return next(u for u in snapshot["users"] if u["username"] == username)


def _default_chat_id(snapshot: dict[str, Any]) -> int:
    return int(snapshot["chats"][0]["id"])


def _send_message(client: TestClient, chat_id: int, user_id: int, text: str = "hi") -> None:
    resp = client.post(f"/api/chats/{chat_id}/messages", json={"user_id": user_id, "text": text})
    assert resp.status_code == 201, resp.text


# --------------------------------------------------------------------- creation


class TestCreateScenario:
    def test_generated_id_follows_the_documented_pattern(self, client: TestClient) -> None:
        resp = client.post("/api/scenarios", json={"name": "core_rules pt"})
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["id"] == "scenario-1"
        assert body["name"] == "core_rules pt"
        assert body["status"] == "running"
        assert body["tags"] == []
        assert body["metadata"] == {}
        assert body["notes"] == []
        assert body["ended_at"] is None
        assert body["message_count"] == 0
        assert body["api_call_count"] == 0

    def test_created_scenario_is_active_by_default(self, client: TestClient) -> None:
        resp = client.post("/api/scenarios", json={"name": "core_rules"})
        scenario_id = resp.json()["id"]
        state = client.get("/api/state").json()
        assert state["active_scenario_id"] == scenario_id

    def test_activate_false_leaves_nothing_active(self, client: TestClient) -> None:
        resp = client.post(
            "/api/scenarios", json={"name": "prepared ahead of time", "activate": False}
        )
        assert resp.status_code == 201
        state = client.get("/api/state").json()
        assert state["active_scenario_id"] is None

    def test_caller_supplied_id_and_full_shape_round_trips(self, client: TestClient) -> None:
        resp = client.post(
            "/api/scenarios",
            json={
                "id": "qa/test_core_rules.py::test_pt",
                "name": "core_rules pt",
                "description": "checks /regras answers in Portuguese",
                "source": "e2e",
                "tags": ["captcha", "join-chain"],
                "metadata": {"nodeid": "qa/test_core_rules.py::test_pt", "group_id": 7},
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["id"] == "qa/test_core_rules.py::test_pt"
        assert body["description"] == "checks /regras answers in Portuguese"
        assert body["source"] == "e2e"
        assert body["tags"] == ["captcha", "join-chain"]
        assert body["metadata"] == {"nodeid": "qa/test_core_rules.py::test_pt", "group_id": 7}

    def test_duplicate_id_is_409(self, client: TestClient) -> None:
        resp = client.post("/api/scenarios", json={"id": "fixed-id", "name": "first"})
        assert resp.status_code == 201, resp.text

        resp = client.post("/api/scenarios", json={"id": "fixed-id", "name": "second"})
        assert resp.status_code == 409

    def test_second_generated_id_does_not_collide_with_the_first(self, client: TestClient) -> None:
        first = client.post("/api/scenarios", json={"name": "one"}).json()
        second = client.post("/api/scenarios", json={"name": "two"}).json()
        assert first["id"] != second["id"]


# --------------------------------------------------------------------- tagging


class TestTagging:
    def test_message_with_no_active_scenario_is_untagged(self, client: TestClient) -> None:
        snapshot = _seed(client, "default")
        chat_id = _default_chat_id(snapshot)
        bob = _user(snapshot, "bob")
        _send_message(client, chat_id, bob["id"])

        state = client.get("/api/state").json()
        message = state["messages"][str(chat_id)][-1]
        assert message["scenario_id"] is None

    def test_api_call_with_no_active_scenario_is_untagged(self, client: TestClient) -> None:
        store().record_api_call("sendMessage", {"chat_id": 1, "text": "hi"})
        assert store().api_calls[-1]["scenario_id"] is None

    def test_message_and_api_call_are_tagged_while_a_scenario_is_active(
        self, client: TestClient
    ) -> None:
        snapshot = _seed(client, "default")
        chat_id = _default_chat_id(snapshot)
        bob = _user(snapshot, "bob")
        scenario_id = client.post("/api/scenarios", json={"name": "core_rules"}).json()["id"]

        _send_message(client, chat_id, bob["id"])
        store().record_api_call("sendMessage", {"chat_id": chat_id, "text": "hi"})

        state = client.get("/api/state").json()
        message = state["messages"][str(chat_id)][-1]
        assert message["scenario_id"] == scenario_id
        assert store().api_calls[-1]["scenario_id"] == scenario_id

    def test_tagging_stops_the_moment_the_scenario_is_deactivated(self, client: TestClient) -> None:
        snapshot = _seed(client, "default")
        chat_id = _default_chat_id(snapshot)
        bob = _user(snapshot, "bob")
        client.post("/api/scenarios", json={"name": "core_rules"})

        resp = client.post("/api/scenarios/deactivate")
        assert resp.status_code == 200
        assert resp.json() == {"active_scenario_id": None}

        _send_message(client, chat_id, bob["id"])
        store().record_api_call("sendMessage", {"chat_id": chat_id})

        state = client.get("/api/state").json()
        message = state["messages"][str(chat_id)][-1]
        assert message["scenario_id"] is None
        assert store().api_calls[-1]["scenario_id"] is None

    def test_deactivate_with_nothing_active_is_a_no_op_not_an_error(
        self, client: TestClient
    ) -> None:
        resp = client.post("/api/scenarios/deactivate")
        assert resp.status_code == 200
        assert resp.json() == {"active_scenario_id": None}

    def test_a_message_already_recorded_is_never_retagged_by_a_later_scenario(
        self, client: TestClient
    ) -> None:
        """The whole point: a message belongs to whatever was running when it
        happened, not to whatever happens to be running when someone reads it
        back later."""
        snapshot = _seed(client, "default")
        chat_id = _default_chat_id(snapshot)
        bob = _user(snapshot, "bob")
        first_id = client.post("/api/scenarios", json={"name": "first"}).json()["id"]
        _send_message(client, chat_id, bob["id"], text="during first")

        client.post("/api/scenarios", json={"name": "second"})
        _send_message(client, chat_id, bob["id"], text="during second")

        state = client.get("/api/state").json()
        messages = state["messages"][str(chat_id)]
        during_first = next(m for m in messages if m["text"] == "during first")
        during_second = next(m for m in messages if m["text"] == "during second")
        assert during_first["scenario_id"] == first_id
        assert during_second["scenario_id"] != first_id

    def test_switching_active_scenario_by_creating_a_new_one_replaces_it(
        self, client: TestClient
    ) -> None:
        client.post("/api/scenarios", json={"name": "first"})
        second_id = client.post("/api/scenarios", json={"name": "second"}).json()["id"]
        state = client.get("/api/state").json()
        assert state["active_scenario_id"] == second_id


# --------------------------------------------------------------------- activate


class TestActivate:
    def test_activate_unknown_scenario_is_404(self, client: TestClient) -> None:
        resp = client.post("/api/scenarios/does-not-exist/activate")
        assert resp.status_code == 404

    def test_activate_switches_back_to_an_earlier_scenario(self, client: TestClient) -> None:
        first_id = client.post("/api/scenarios", json={"name": "first"}).json()["id"]
        client.post("/api/scenarios", json={"name": "second"})

        resp = client.post(f"/api/scenarios/{first_id}/activate")
        assert resp.status_code == 200
        assert resp.json()["id"] == first_id
        state = client.get("/api/state").json()
        assert state["active_scenario_id"] == first_id


# --------------------------------------------------------------------- notes


class TestNotes:
    def test_note_defaults_to_info_level_and_is_timestamped(self, client: TestClient) -> None:
        scenario_id = client.post("/api/scenarios", json={"name": "core_rules"}).json()["id"]

        resp = client.post(f"/api/scenarios/{scenario_id}/notes", json={"text": "captcha shown"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body["notes"]) == 1
        note = body["notes"][0]
        assert note["text"] == "captcha shown"
        assert note["level"] == "info"
        assert isinstance(note["at"], float)

    def test_note_level_can_be_set_explicitly(self, client: TestClient) -> None:
        scenario_id = client.post("/api/scenarios", json={"name": "core_rules"}).json()["id"]
        resp = client.post(
            f"/api/scenarios/{scenario_id}/notes",
            json={"text": "retrying join", "level": "warn"},
        )
        assert resp.json()["notes"][0]["level"] == "warn"

    def test_notes_accumulate_in_order(self, client: TestClient) -> None:
        scenario_id = client.post("/api/scenarios", json={"name": "core_rules"}).json()["id"]
        client.post(f"/api/scenarios/{scenario_id}/notes", json={"text": "first"})
        resp = client.post(f"/api/scenarios/{scenario_id}/notes", json={"text": "second"})
        assert [n["text"] for n in resp.json()["notes"]] == ["first", "second"]

    def test_note_on_unknown_scenario_is_404(self, client: TestClient) -> None:
        resp = client.post("/api/scenarios/does-not-exist/notes", json={"text": "x"})
        assert resp.status_code == 404


# --------------------------------------------------------------------- patch


class TestPatch:
    def test_patch_merges_metadata_key_by_key(self, client: TestClient) -> None:
        scenario_id = client.post(
            "/api/scenarios",
            json={"name": "core_rules", "metadata": {"nodeid": "test_x", "group_id": 1}},
        ).json()["id"]

        resp = client.patch(f"/api/scenarios/{scenario_id}", json={"metadata": {"outcome": "ok"}})
        assert resp.status_code == 200, resp.text
        metadata = resp.json()["metadata"]
        assert metadata == {"nodeid": "test_x", "group_id": 1, "outcome": "ok"}

    def test_patch_metadata_overwrites_only_the_given_keys(self, client: TestClient) -> None:
        scenario_id = client.post(
            "/api/scenarios", json={"name": "core_rules", "metadata": {"a": 1, "b": 2}}
        ).json()["id"]
        resp = client.patch(f"/api/scenarios/{scenario_id}", json={"metadata": {"a": 99}})
        assert resp.json()["metadata"] == {"a": 99, "b": 2}

    def test_patch_tags_replaces_rather_than_merges(self, client: TestClient) -> None:
        scenario_id = client.post(
            "/api/scenarios", json={"name": "core_rules", "tags": ["a", "b"]}
        ).json()["id"]
        resp = client.patch(f"/api/scenarios/{scenario_id}", json={"tags": ["c"]})
        assert resp.json()["tags"] == ["c"]

    def test_patch_updates_status_and_description(self, client: TestClient) -> None:
        scenario_id = client.post("/api/scenarios", json={"name": "core_rules"}).json()["id"]
        resp = client.patch(
            f"/api/scenarios/{scenario_id}",
            json={"status": "failed", "description": "captcha never appeared"},
        )
        body = resp.json()
        assert body["status"] == "failed"
        assert body["description"] == "captcha never appeared"

    def test_patch_omitted_fields_are_left_alone(self, client: TestClient) -> None:
        scenario_id = client.post(
            "/api/scenarios", json={"name": "core_rules", "tags": ["a"]}
        ).json()["id"]
        resp = client.patch(f"/api/scenarios/{scenario_id}", json={"status": "passed"})
        body = resp.json()
        assert body["status"] == "passed"
        assert body["tags"] == ["a"]
        assert body["name"] == "core_rules"

    def test_patch_unknown_scenario_is_404(self, client: TestClient) -> None:
        resp = client.patch("/api/scenarios/does-not-exist", json={"status": "failed"})
        assert resp.status_code == 404


# --------------------------------------------------------------------- end


class TestEnd:
    def test_end_defaults_to_closed_when_still_running(self, client: TestClient) -> None:
        scenario_id = client.post("/api/scenarios", json={"name": "core_rules"}).json()["id"]
        resp = client.post(f"/api/scenarios/{scenario_id}/end")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "closed"
        assert body["ended_at"] is not None

    def test_end_applies_an_explicit_status(self, client: TestClient) -> None:
        scenario_id = client.post("/api/scenarios", json={"name": "core_rules"}).json()["id"]
        resp = client.post(f"/api/scenarios/{scenario_id}/end", json={"status": "passed"})
        assert resp.json()["status"] == "passed"

    def test_end_does_not_override_a_status_already_set_away_from_running(
        self, client: TestClient
    ) -> None:
        scenario_id = client.post("/api/scenarios", json={"name": "core_rules"}).json()["id"]
        client.patch(f"/api/scenarios/{scenario_id}", json={"status": "failed"})
        resp = client.post(f"/api/scenarios/{scenario_id}/end")
        assert resp.json()["status"] == "failed"

    def test_ending_the_active_scenario_clears_active_scenario_id(self, client: TestClient) -> None:
        scenario_id = client.post("/api/scenarios", json={"name": "core_rules"}).json()["id"]
        client.post(f"/api/scenarios/{scenario_id}/end")
        state = client.get("/api/state").json()
        assert state["active_scenario_id"] is None

    def test_ending_an_inactive_scenario_leaves_the_active_one_untouched(
        self, client: TestClient
    ) -> None:
        first_id = client.post("/api/scenarios", json={"name": "first"}).json()["id"]
        second_id = client.post("/api/scenarios", json={"name": "second"}).json()["id"]

        client.post(f"/api/scenarios/{first_id}/end")

        state = client.get("/api/state").json()
        assert state["active_scenario_id"] == second_id

    def test_end_unknown_scenario_is_404(self, client: TestClient) -> None:
        resp = client.post("/api/scenarios/does-not-exist/end")
        assert resp.status_code == 404


# --------------------------------------------------------------------- counts


class TestCounts:
    def test_message_count_and_api_call_count_reflect_only_this_scenario(
        self, client: TestClient
    ) -> None:
        snapshot = _seed(client, "default")
        chat_id = _default_chat_id(snapshot)
        bob = _user(snapshot, "bob")
        scenario_id = client.post("/api/scenarios", json={"name": "core_rules"}).json()["id"]

        _send_message(client, chat_id, bob["id"], text="one")
        _send_message(client, chat_id, bob["id"], text="two")
        store().record_api_call("sendMessage", {"chat_id": chat_id, "text": "one"})

        client.post("/api/scenarios", json={"name": "unrelated"})
        _send_message(client, chat_id, bob["id"], text="not counted")

        state = client.get("/api/state").json()
        scenario = next(s for s in state["scenarios"] if s["id"] == scenario_id)
        assert scenario["message_count"] == 2
        assert scenario["api_call_count"] == 1

    def test_fresh_scenario_has_zero_counts(self, client: TestClient) -> None:
        resp = client.post("/api/scenarios", json={"name": "core_rules"})
        assert resp.json()["message_count"] == 0
        assert resp.json()["api_call_count"] == 0


# --------------------------------------------------------------------- snapshot


class TestSnapshot:
    def test_scenarios_are_ordered_by_started_at(self, client: TestClient) -> None:
        first_id = client.post("/api/scenarios", json={"name": "first"}).json()["id"]
        second_id = client.post("/api/scenarios", json={"name": "second"}).json()["id"]
        third_id = client.post("/api/scenarios", json={"name": "third"}).json()["id"]

        state = client.get("/api/state").json()
        assert [s["id"] for s in state["scenarios"]] == [first_id, second_id, third_id]

    def test_reset_clears_scenarios_and_active_id(self, client: TestClient) -> None:
        client.post("/api/scenarios", json={"name": "core_rules"})
        resp = client.post("/api/reset")
        assert resp.status_code == 200
        state = client.get("/api/state").json()
        assert state["scenarios"] == []
        assert state["active_scenario_id"] is None
