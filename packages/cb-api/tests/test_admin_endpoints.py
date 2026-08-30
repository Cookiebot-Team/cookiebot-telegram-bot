"""`cb_api.routers.admin` — who runs the deployment, and what that lets them see.

Two things are under test and they are not the same thing.

**The boundary.** An owner gets in; a group admin who is not an owner does not,
and gets **403** rather than the 404 the group endpoints answer with (there is
no chat id to hide behind `/admin/overview`). An owner whose token predates
their ownership — no `admin:read` — is refused too, with the challenge that
tells the client to ask for a better token.

**The shapes.** Every response is a declared model, because the Mini App is
generated from `/openapi.json`; a field renamed here without the model noticing
would reach a client as a silently missing chart.

The database is faked. That the SQL means what these fakes pretend it means is
`qa/integration/test_platform_analytics.py`'s job.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cb_api import keys, security
from cb_api.routers import admin as router_module
from cb_core import platform_analytics, tenancy
from cb_core.settings import get_settings

PEM = keys.generate_private_pem()
KID = "cookiebot-test"

TENANT_OWNER = 777
ENV_OWNER = 555
GROUP_ADMIN = 4242
STRANGER = 9999

ADMIN_SCOPES = "groups:read groups:write audit:read admin:read"
TODAY = date(2026, 8, 20)


def _token(subject: int, *, scope: str | None = ADMIN_SCOPES) -> str:
    claims: dict[str, Any] = {
        "sub": str(subject),
        "iat": 1_700_000_000,
        "exp": 4_102_444_800,
        "kid": KID,
    }
    if scope is not None:
        claims["scope"] = scope
    return jwt.encode(claims, PEM, algorithm="RS256", headers={"kid": KID})


def _auth(subject: int = TENANT_OWNER, *, scope: str | None = ADMIN_SCOPES) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(subject, scope=scope)}"}


@pytest.fixture(autouse=True)
def _keys(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _published() -> tuple[keys.SigningKey, ...]:
        return (keys.SigningKey(kid=KID, private_pem=PEM),)

    monkeypatch.setattr(keys, "published_keys", _published)


@pytest.fixture(autouse=True)
def _owners(monkeypatch: pytest.MonkeyPatch) -> None:
    """One owner from the tenant row, one from `CB_OWNER_ID`. Both are real
    owners and the endpoints must not tell them apart."""

    async def _tenant(tenant_id: str) -> tenancy.Tenant:
        return tenancy.Tenant(
            tenant_id=tenancy.DEFAULT_TENANT,
            display_name="Cookiebot",
            owner_ids=(TENANT_OWNER,),
            monthly_llm_budget_usd=50.0,
            disabled_commands=frozenset({"battle"}),
        )

    monkeypatch.setattr(tenancy.registry, "by_id", _tenant)
    monkeypatch.setattr(get_settings(), "owner_id", ENV_OWNER, raising=False)


@pytest.fixture(autouse=True)
def _data(monkeypatch: pytest.MonkeyPatch) -> None:
    """The rollups, as `cb_core.platform_analytics` would have returned them."""

    async def _daily(start: date, end: date) -> tuple[platform_analytics.PlatformDay, ...]:
        return (
            platform_analytics.PlatformDay(
                day=start,
                groups=2,
                messages=100,
                commands=20,
                joins=5,
                leaves=1,
                captcha_issued=4,
                captcha_solved=3,
                active_users=17,
                errors=2,
                p95_latency_ms=120,
                llm_tokens=1_000,
                llm_cost_usd=1.5,
            ),
            platform_analytics.PlatformDay(
                day=end,
                groups=3,
                messages=50,
                commands=10,
                joins=2,
                leaves=0,
                captcha_issued=0,
                captcha_solved=0,
                active_users=9,
                errors=0,
                p95_latency_ms=90,
                llm_tokens=500,
                llm_cost_usd=0.75,
            ),
        )

    async def _reach() -> platform_analytics.Reach:
        return platform_analytics.Reach(groups=12, groups_left=3, members=940, admins=31)

    async def _top(
        start: date, end: date, *, limit: int = 20
    ) -> tuple[platform_analytics.GroupActivity, ...]:
        return (
            platform_analytics.GroupActivity(
                group_id=-1001,
                title="Busy Chat",
                username="busy",
                messages=90,
                commands=18,
                errors=1,
                peak_active_users=14,
                llm_cost_usd=1.25,
            ),
        )

    async def _commands(
        start: date, end: date, *, limit: int = 20
    ) -> tuple[platform_analytics.PlatformCommand, ...]:
        return (
            platform_analytics.PlatformCommand(
                command="dice", invocations=30, errors=0, groups=4, p95_latency_ms=40
            ),
        )

    async def _llm(start: date, end: date) -> tuple[platform_analytics.PlatformLlmCost, ...]:
        return (
            platform_analytics.PlatformLlmCost(
                provider="anthropic",
                model="claude-opus-5",
                calls=12,
                input_tokens=900,
                output_tokens=600,
                cost_usd=2.25,
                refusals=1,
                errors=0,
            ),
        )

    async def _directory(
        *,
        limit: int = 50,
        after: int | None = None,
        search: str | None = None,
        active_only: bool = True,
    ) -> tuple[platform_analytics.GroupRow, ...]:
        rows = tuple(
            platform_analytics.GroupRow(
                group_id=-1000 - index,
                title=f"Group {index}",
                username=None,
                chat_type="supergroup",
                skin="cookiebot",
                joined_at=datetime(2026, 1, 1, tzinfo=UTC),
                left_at=None,
                members=10 * index,
                admins=index,
            )
            for index in range(1, 4)
        )
        return rows[:limit]

    monkeypatch.setattr(router_module.platform_analytics, "daily", _daily)
    monkeypatch.setattr(router_module.platform_analytics, "reach", _reach)
    monkeypatch.setattr(router_module.platform_analytics, "top_groups", _top)
    monkeypatch.setattr(router_module.platform_analytics, "commands", _commands)
    monkeypatch.setattr(router_module.platform_analytics, "llm_costs", _llm)
    monkeypatch.setattr(router_module.platform_analytics, "directory", _directory)


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(router_module.router)
    with TestClient(app) as test_client:
        yield test_client


# ---------------------------------------------------------------- who gets in


def test_a_tenant_owner_reads_the_overview(client: TestClient) -> None:
    assert client.get("/admin/overview", headers=_auth()).status_code == 200


def test_the_env_owner_is_an_owner_too(client: TestClient) -> None:
    """`CB_OWNER_ID` is what the owner-only Telegram commands answer to. An
    owner who can `/broadcast` in chat and gets a 403 here would be a
    discrepancy nobody would guess at."""
    assert client.get("/admin/overview", headers=_auth(ENV_OWNER)).status_code == 200


def test_a_group_admin_is_not_a_bot_admin(client: TestClient) -> None:
    response = client.get("/admin/overview", headers=_auth(GROUP_ADMIN))
    assert response.status_code == 403
    assert "owner" in response.json()["detail"]


def test_a_stranger_is_refused(client: TestClient) -> None:
    assert client.get("/admin/groups", headers=_auth(STRANGER)).status_code == 403


def test_no_token_is_401_with_a_challenge(client: TestClient) -> None:
    response = client.get("/admin/overview")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_an_owner_without_the_scope_is_told_which_one(client: TestClient) -> None:
    """An owner holding a token minted before they were one — or a `/login`
    token, which carries no scopes at all — can fix this by asking
    `/oauth2/token` again, and the challenge says so."""
    response = client.get("/admin/overview", headers=_auth(TENANT_OWNER, scope="groups:read"))
    assert response.status_code == 403
    assert 'scope="admin:read"' in response.headers["www-authenticate"]


def test_a_legacy_console_token_cannot_reach_the_fleet(client: TestClient) -> None:
    response = client.get("/admin/overview", headers=_auth(TENANT_OWNER, scope=None))
    assert response.status_code == 403


@pytest.mark.parametrize(
    "path",
    [
        "/admin/overview",
        "/admin/analytics/daily",
        "/admin/analytics/groups",
        "/admin/analytics/commands",
        "/admin/analytics/llm",
        "/admin/groups",
        "/admin/tenant",
    ],
)
def test_every_endpoint_is_behind_the_boundary(client: TestClient, path: str) -> None:
    """Parameterised rather than trusted to the shared dependency: a route
    added later without `Admin` on it would be an open fleet-wide read, and
    that is exactly the mistake nobody notices in review."""
    assert client.get(path).status_code == 401
    assert client.get(path, headers=_auth(GROUP_ADMIN)).status_code == 403


# -------------------------------------------------------------- what they see


def test_the_overview_carries_reach_totals_and_the_budget(client: TestClient) -> None:
    body = client.get("/admin/overview", headers=_auth()).json()
    assert body["tenant_id"] == "cookiebot"
    assert body["reach"] == {"groups": 12, "groups_left": 3, "members": 940, "admins": 31}
    assert body["totals"]["messages"] == 150
    assert body["totals"]["peak_groups"] == 3
    assert body["budget"]["monthly_llm_budget_usd"] == 50.0
    assert body["budget"]["spent_usd"] == 2.25
    assert body["budget"]["remaining_usd"] == 47.75


def test_a_tenant_with_no_budget_reports_no_remainder(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`null`, not `0` — "no budget configured" and "nothing left" are opposite
    facts and a dashboard must not draw them the same."""

    async def _tenant(tenant_id: str) -> tenancy.Tenant:
        return tenancy.Tenant(
            tenant_id=tenancy.DEFAULT_TENANT, display_name="Cookiebot", owner_ids=(TENANT_OWNER,)
        )

    monkeypatch.setattr(tenancy.registry, "by_id", _tenant)
    body = client.get("/admin/overview", headers=_auth()).json()
    assert body["budget"]["monthly_llm_budget_usd"] is None
    assert body["budget"]["remaining_usd"] is None


def test_the_solve_rate_is_null_when_nobody_was_challenged(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _daily(start: date, end: date) -> tuple[platform_analytics.PlatformDay, ...]:
        return ()

    monkeypatch.setattr(router_module.platform_analytics, "daily", _daily)
    body = client.get("/admin/overview", headers=_auth()).json()
    assert body["totals"]["captcha_solve_rate"] is None
    assert body["totals"]["days"] == 0


def test_daily_rows_say_how_many_groups_were_active(client: TestClient) -> None:
    body = client.get("/admin/analytics/daily", headers=_auth()).json()
    assert [row["groups"] for row in body["days"]] == [2, 3]


def test_the_command_table_counts_groups_not_just_calls(client: TestClient) -> None:
    body = client.get("/admin/analytics/commands", headers=_auth()).json()
    assert body["commands"][0] == {
        "command": "dice",
        "invocations": 30,
        "errors": 0,
        "groups": 4,
        "p95_latency_ms": 40,
    }


def test_the_llm_total_is_the_sum_of_its_models(client: TestClient) -> None:
    body = client.get("/admin/analytics/llm", headers=_auth()).json()
    assert body["total_cost_usd"] == 2.25
    assert body["models"][0]["provider"] == "anthropic"


def test_the_leaderboard_names_the_group(client: TestClient) -> None:
    body = client.get("/admin/analytics/groups?limit=5", headers=_auth()).json()
    assert body["groups"][0]["title"] == "Busy Chat"


def test_the_directory_hands_back_a_cursor_only_when_the_page_was_full(
    client: TestClient,
) -> None:
    """`next_after` is null on the last page. A client that pages until the
    cursor is null is the contract; one that pages until the list is empty
    makes one wasted request per listing."""
    full = client.get("/admin/groups?limit=3", headers=_auth()).json()
    assert full["next_after"] == full["groups"][-1]["group_id"]
    partial = client.get("/admin/groups?limit=50", headers=_auth()).json()
    assert partial["next_after"] is None


def test_the_tenant_endpoint_never_returns_a_bot_token(client: TestClient) -> None:
    """An endpoint that returned bot tokens would be one stolen owner token
    away from being the whole deployment."""
    response = client.get("/admin/tenant", headers=_auth())
    assert response.status_code == 200
    body = response.json()
    assert "bot_tokens" not in body
    assert body["disabled_commands"] == ["battle"]
    # Both kinds of owner appear, so an owner reading the page can see who else
    # holds the same power.
    assert body["owner_ids"] == [ENV_OWNER, TENANT_OWNER]


# ------------------------------------------------------------------ windows


def test_a_reversed_window_is_a_400(client: TestClient) -> None:
    response = client.get(
        "/admin/analytics/daily",
        params={"start": "2026-03-01", "end": "2026-02-01"},
        headers=_auth(),
    )
    assert response.status_code == 400


def test_a_window_longer_than_a_year_is_a_400(client: TestClient) -> None:
    start = TODAY - timedelta(days=400)
    response = client.get(
        "/admin/analytics/daily",
        params={"start": start.isoformat(), "end": TODAY.isoformat()},
        headers=_auth(),
    )
    assert response.status_code == 400


def test_the_resolved_window_is_echoed_back(client: TestClient) -> None:
    """The caller may have named one end or neither; a chart labels the axis it
    got, not the one it asked for."""
    body = client.get(
        "/admin/analytics/daily", params={"end": "2026-08-20"}, headers=_auth()
    ).json()
    assert body["end"] == "2026-08-20"
    assert body["start"] == "2026-07-22"


# ------------------------------------------------------- the scope is granted


@pytest.mark.parametrize(
    ("user_id", "expected"),
    [(TENANT_OWNER, True), (ENV_OWNER, True), (GROUP_ADMIN, False), (STRANGER, False)],
)
async def test_only_owners_are_bot_admins(user_id: int, expected: bool) -> None:
    """The predicate `/oauth2/token` grants `admin:read` from, and `/me`
    reports. Tested directly because both callers are one `if` away from
    granting it to everybody."""
    assert await security.is_bot_admin(user_id) is expected
