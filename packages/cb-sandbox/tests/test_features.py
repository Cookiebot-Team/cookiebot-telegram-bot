"""Grouping a run by feature — the axis validation actually happens along.

A per-test result list answers "which check failed". It cannot answer "is this
behaviour correct", because that is a question about one feature and every
scenario that touched it, and it cannot answer "did we check this at all",
because a feature nobody exercised has no row in a report of tests that ran.

Both of those are what `GET /api/features` is for, and both are what these
tests pin down.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from cb_sandbox.config import DEFAULT_CONFIG, FeatureSpec, set_config
from cb_sandbox.control_api import router
from cb_sandbox.state import store
from fastapi import FastAPI
from fastapi.testclient import TestClient

_FEATURES = (
    FeatureSpec(id="rules", title="Rules", status="done", commands=("/rules",), tags=("regras",)),
    FeatureSpec(id="captcha", title="Captcha", status="done", tags=("join_chain",)),
    FeatureSpec(id="giveaways", title="Giveaways", status="planned"),
)


@pytest.fixture
def client() -> TestClient:
    set_config(replace(DEFAULT_CONFIG, features=_FEATURES))
    store().reset()
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _scenario(client: TestClient, **body: Any) -> dict[str, Any]:
    resp = client.post("/api/scenarios", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _feature(client: TestClient, feature_id: str) -> dict[str, Any]:
    features = client.get("/api/features").json()
    return next(f for f in features if f["id"] == feature_id)


class TestFiling:
    def test_an_explicit_feature_is_honoured(self, client: TestClient) -> None:
        scenario = _scenario(client, id="s1", name="check", feature="rules")
        assert scenario["feature"] == "rules"
        assert _feature(client, "rules")["scenario_ids"] == ["s1"]

    def test_a_tag_matching_a_feature_files_the_scenario(self, client: TestClient) -> None:
        """The grandfathering path: a suite that already tags its runs gets
        feature grouping without a line of change. Without this, adopting the
        feature view would mean editing every existing test first, which is
        exactly the cost that stops people adopting it."""
        _scenario(client, id="s2", name="join chain", tags=["join_chain", "en"])
        assert _feature(client, "captcha")["scenario_ids"] == ["s2"]

    def test_an_explicit_feature_beats_a_conflicting_tag(self, client: TestClient) -> None:
        scenario = _scenario(client, id="s3", name="x", feature="rules", tags=["join_chain"])
        assert scenario["feature"] == "rules"

    def test_a_scenario_matching_nothing_is_unfiled_rather_than_guessed(
        self, client: TestClient
    ) -> None:
        scenario = _scenario(client, id="s4", name="x", tags=["nothing_matches"])
        assert scenario["feature"] is None

    def test_filing_is_resolved_on_read_not_frozen_at_creation(self, client: TestClient) -> None:
        """Adding a feature to the config should retroactively group the
        scenarios that already match it. Freezing the answer at creation would
        mean a newly declared feature always shows zero scenarios until the
        suite is re-run, which reads as "untested" when it isn't."""
        _scenario(client, id="s5", name="x", tags=["shipping"])
        assert client.get("/api/state").json()["scenarios"][0]["feature"] is None

        set_config(
            replace(
                DEFAULT_CONFIG,
                features=(*_FEATURES, FeatureSpec(id="shipping", title="Shipping")),
            )
        )
        assert _feature(client, "shipping")["scenario_count"] == 1

    def test_patching_a_scenario_can_refile_it(self, client: TestClient) -> None:
        _scenario(client, id="s6", name="x")
        resp = client.patch("/api/scenarios/s6", json={"feature": "captcha"})
        assert resp.status_code == 200
        assert resp.json()["feature"] == "captcha"


class TestRollup:
    def test_status_counts_use_the_callers_own_vocabulary(self, client: TestClient) -> None:
        """Not normalised to a fixed set: a suite that reports "flaky" should
        see "flaky" rather than have it folded into a word this server made up
        — the point of the rollup is to report the run, not to reinterpret it."""
        for index, status in enumerate(("passed", "passed", "failed", "flaky")):
            _scenario(client, id=f"r{index}", name=f"r{index}", feature="rules", activate=False)
            client.post(f"/api/scenarios/r{index}/end", json={"status": status})
        counts = _feature(client, "rules")["status_counts"]
        assert counts == {"passed": 2, "failed": 1, "flaky": 1}

    def test_a_feature_nobody_exercised_still_has_a_row(self, client: TestClient) -> None:
        """The single most valuable row in this view. A feature with no
        scenarios is invisible in every per-test report ever written — it
        looks exactly like a feature that passed."""
        giveaways = _feature(client, "giveaways")
        assert giveaways["scenario_count"] == 0
        assert giveaways["status_counts"] == {}
        assert giveaways["status"] == "planned"

    def test_message_and_call_counts_aggregate_across_a_features_scenarios(
        self, client: TestClient
    ) -> None:
        snapshot = client.post("/api/seed", json={"scenario": "default"}).json()
        chat_id = snapshot["chats"][0]["id"]
        bob = next(u for u in snapshot["users"] if u["username"] == "bob")

        for index in range(2):
            _scenario(client, id=f"m{index}", name=f"m{index}", feature="rules")
            client.post(f"/api/chats/{chat_id}/messages", json={"user_id": bob["id"], "text": "hi"})
            client.post(f"/api/scenarios/m{index}/end", json={"status": "passed"})

        assert _feature(client, "rules")["message_count"] == 2

    def test_the_snapshot_carries_the_same_rollup_as_the_endpoint(self, client: TestClient) -> None:
        """Two sources for the same numbers is how a UI ends up showing a
        feature summary that disagrees with the scenario list beside it."""
        _scenario(client, id="s7", name="x", feature="rules")
        from_snapshot = client.get("/api/state").json()["features"]
        from_endpoint = client.get("/api/features").json()
        assert from_snapshot == from_endpoint


class TestKit:
    def test_the_kit_describes_the_configured_bot(self, client: TestClient) -> None:
        kit = client.get("/api/kit").json()
        assert kit["bot"]["username"] == DEFAULT_CONFIG.bot.username
        assert [f["id"] for f in kit["features"]] == ["rules", "captcha", "giveaways"]
        assert [s["name"] for s in kit["seeds"]] == DEFAULT_CONFIG.seed_names()

    def test_the_kit_reports_where_its_config_came_from(self, client: TestClient) -> None:
        """A palette that disagrees with the bot is almost always a stale or
        unexpected config file. Naming the file is the difference between a
        two-minute fix and an afternoon."""
        assert client.get("/api/kit").json()["config_source"] == "built-in defaults"
