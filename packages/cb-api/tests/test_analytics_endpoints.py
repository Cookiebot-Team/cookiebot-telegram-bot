"""`cb_api.routers.analytics` over HTTP — who gets in, who does not, and what
the four bodies look like.

Same shape as `test_login_endpoints.py`: the router is mounted on a bare app,
so this layer needs no pool, cache or object store. The queries are faked here
and exercised for real against Citus in `qa/integration/test_analytics.py`;
what is under test is the wire contract and the authorisation boundary.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from typing import Any

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cb_api import keys, security
from cb_api.routers import analytics as router_module
from cb_core import analytics as core_analytics
from cb_core import tenancy

PEM = keys.generate_private_pem()
KID = "cookiebot-test"

GROUP_ID = -1001234567890
ADMIN_ID = 4242
STRANGER_ID = 9999
OWNER_ID = 777


def _token(subject: int, *, pem: str = PEM, kid: str = KID) -> str:
    return jwt.encode(
        {"sub": str(subject), "iat": 1_700_000_000, "exp": 4_102_444_800, "kid": kid},
        pem,
        algorithm="RS256",
        headers={"kid": kid},
    )


def _row(day: date, **overrides: Any) -> core_analytics.DailyStats:
    fields: dict[str, Any] = {
        "day": day,
        "messages": 12,
        "commands": 3,
        "joins": 1,
        "leaves": 0,
        "captcha_issued": 2,
        "captcha_solved": 1,
        "active_users": 5,
        "errors": 0,
        "p95_latency_ms": 140,
        "llm_tokens": 900,
        "llm_cost_usd": 0.12,
    }
    fields.update(overrides)
    return core_analytics.DailyStats(**fields)


@pytest.fixture(autouse=True)
def _keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """One published key, no database. `published_keys` is what
    `security._decode` walks, so faking it here is the whole key seam."""

    async def _published() -> tuple[keys.SigningKey, ...]:
        return (keys.SigningKey(kid=KID, private_pem=PEM),)

    monkeypatch.setattr(keys, "published_keys", _published)


@pytest.fixture(autouse=True)
def _admins(monkeypatch: pytest.MonkeyPatch) -> None:
    """`ADMIN_ID` administers `GROUP_ID`; nobody else administers anything."""

    async def _is_admin(group_id: int, user_id: int) -> bool:
        return group_id == GROUP_ID and user_id == ADMIN_ID

    monkeypatch.setattr(security, "_is_group_admin", _is_admin)

    async def _tenant(tenant_id: str) -> tenancy.Tenant:
        return tenancy.Tenant(
            tenant_id=tenancy.DEFAULT_TENANT, display_name="Cookiebot", owner_ids=(OWNER_ID,)
        )

    monkeypatch.setattr(tenancy.registry, "by_id", _tenant)


@pytest.fixture(autouse=True)
def _queries(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _daily(
        group_id: int, start: date, end: date
    ) -> tuple[core_analytics.DailyStats, ...]:
        return (_row(start), _row(end, messages=8, active_users=9))

    async def _commands(
        group_id: int, start: date, end: date, *, limit: int = 20
    ) -> tuple[core_analytics.CommandStats, ...]:
        return (
            core_analytics.CommandStats(
                command="meme", invocations=40, errors=1, p95_latency_ms=310
            ),
            core_analytics.CommandStats(
                command="battle", invocations=9, errors=0, p95_latency_ms=None
            ),
        )[:limit]

    async def _llm(group_id: int, start: date, end: date) -> tuple[core_analytics.LlmCost, ...]:
        return (
            core_analytics.LlmCost(
                provider="anthropic",
                model="claude-sonnet-5",
                calls=12,
                input_tokens=1000,
                output_tokens=500,
                cost_usd=0.34,
                refusals=1,
                errors=0,
            ),
        )

    monkeypatch.setattr(router_module.analytics, "daily", _daily)
    monkeypatch.setattr(router_module.analytics, "commands", _commands)
    monkeypatch.setattr(router_module.analytics, "llm_costs", _llm)


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(router_module.router)
    with TestClient(app) as test_client:
        yield test_client


def _auth(subject: int = ADMIN_ID) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(subject)}"}


class TestAuthentication:
    def test_no_token_is_401_with_a_challenge(self, client: TestClient) -> None:
        response = client.get(f"/groups/{GROUP_ID}/analytics/summary")
        assert response.status_code == 401
        assert "Bearer" in response.headers.get("www-authenticate", "")

    def test_a_token_signed_by_another_key_is_401(self, client: TestClient) -> None:
        other = keys.generate_private_pem()
        headers = {"Authorization": f"Bearer {_token(ADMIN_ID, pem=other)}"}
        assert (
            client.get(f"/groups/{GROUP_ID}/analytics/summary", headers=headers).status_code == 401
        )

    def test_a_garbage_token_is_401(self, client: TestClient) -> None:
        headers = {"Authorization": "Bearer not-a-jwt"}
        assert (
            client.get(f"/groups/{GROUP_ID}/analytics/summary", headers=headers).status_code == 401
        )


class TestAuthorisation:
    def test_a_group_admin_is_allowed(self, client: TestClient) -> None:
        assert (
            client.get(f"/groups/{GROUP_ID}/analytics/summary", headers=_auth()).status_code == 200
        )

    def test_a_tenant_owner_is_allowed(self, client: TestClient) -> None:
        response = client.get(f"/groups/{GROUP_ID}/analytics/summary", headers=_auth(OWNER_ID))
        assert response.status_code == 200

    def test_a_stranger_gets_404_not_403(self, client: TestClient) -> None:
        """Whether a given chat id is known to this deployment is not something
        an arbitrary logged-in user should be able to probe."""
        response = client.get(f"/groups/{GROUP_ID}/analytics/summary", headers=_auth(STRANGER_ID))
        assert response.status_code == 404

    def test_an_admin_of_one_group_cannot_read_another(self, client: TestClient) -> None:
        response = client.get("/groups/-100999/analytics/summary", headers=_auth())
        assert response.status_code == 404


class TestBodies:
    def test_daily_returns_the_window_and_its_rows(self, client: TestClient) -> None:
        response = client.get(
            f"/groups/{GROUP_ID}/analytics/daily?start=2026-01-01&end=2026-01-02",
            headers=_auth(),
        )
        body = response.json()
        assert body["group_id"] == GROUP_ID
        assert body["start"] == "2026-01-01"
        assert body["end"] == "2026-01-02"
        assert [row["day"] for row in body["days"]] == ["2026-01-01", "2026-01-02"]
        assert body["days"][0]["messages"] == 12

    def test_commands_honours_the_limit(self, client: TestClient) -> None:
        response = client.get(f"/groups/{GROUP_ID}/analytics/commands?limit=1", headers=_auth())
        assert [row["command"] for row in response.json()["commands"]] == ["meme"]

    def test_a_limit_over_the_cap_is_rejected(self, client: TestClient) -> None:
        response = client.get(f"/groups/{GROUP_ID}/analytics/commands?limit=500", headers=_auth())
        assert response.status_code == 422

    def test_llm_totals_the_models(self, client: TestClient) -> None:
        body = client.get(f"/groups/{GROUP_ID}/analytics/llm", headers=_auth()).json()
        assert body["total_cost_usd"] == 0.34
        assert body["models"][0]["model"] == "claude-sonnet-5"

    def test_summary_folds_the_days(self, client: TestClient) -> None:
        body = client.get(
            f"/groups/{GROUP_ID}/analytics/summary?start=2026-01-01&end=2026-01-02",
            headers=_auth(),
        ).json()
        assert body["days"] == 2
        assert body["messages"] == 20
        assert body["peak_active_users"] == 9
        assert body["captcha_solve_rate"] == 0.5

    def test_a_reversed_window_is_a_400(self, client: TestClient) -> None:
        response = client.get(
            f"/groups/{GROUP_ID}/analytics/daily?start=2026-02-03&end=2026-02-01",
            headers=_auth(),
        )
        assert response.status_code == 400


class TestKeyOrdering:
    def test_the_kid_named_by_the_header_is_tried_first(self) -> None:
        """A `kid` is a hint from an unverified header, so every key is still
        tried — but the usual request should cost one RSA verification, not
        one per published key."""
        wanted = keys.SigningKey(kid=KID, private_pem=PEM)
        other = keys.SigningKey(kid="older", private_pem=keys.generate_private_pem())
        ordered = security._ordered_keys(_token(ADMIN_ID), (other, wanted))  # noqa: SLF001
        assert [key.kid for key in ordered] == [KID, "older"]

    def test_an_unreadable_header_falls_back_to_the_published_order(self) -> None:
        published = (
            keys.SigningKey(kid="a", private_pem=PEM),
            keys.SigningKey(kid="b", private_pem=PEM),
        )
        assert security._ordered_keys("not-a-jwt", published) == list(published)  # noqa: SLF001

    def test_a_token_whose_kid_is_unknown_still_verifies_against_the_right_key(
        self, client: TestClient
    ) -> None:
        """The header is not trusted: naming a kid nobody has must not skip the
        key that actually signed the token."""
        token = jwt.encode(
            {"sub": str(ADMIN_ID), "iat": 1_700_000_000, "exp": 4_102_444_800},
            PEM,
            algorithm="RS256",
            headers={"kid": "who-knows"},
        )
        response = client.get(
            f"/groups/{GROUP_ID}/analytics/summary", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 200
