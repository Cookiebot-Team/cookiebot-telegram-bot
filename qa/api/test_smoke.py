"""Smoke: does the deployment I just started actually answer?

The narrowest layer and the only one that talks over a socket. It does not
re-test behaviour — `test_integration.py` does that in-process, faster and
without a server. What only this layer can catch is everything *between* the
code and a caller: a process that will not boot, a `.env` that points at the
wrong database, a middleware that swallows the `WWW-Authenticate` header, a
reverse proxy in front of it, a schema that was never migrated.

    uv run scripts/qa_setup.py     # starts an API and seeds the demo data
    python scripts/cb.py api-test  # this file, plus the other two

**It skips loudly rather than passing quietly.** With no server listening, or no
seeded session to read ids from, every test here skips with the command that
would fix it. A smoke suite that silently passes when it tested nothing is worse
than no smoke suite: it is a green tick over an untested deployment.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from qa.api.auth import MINIAPP_GRANT, init_data

pytestmark = pytest.mark.smoke

ROOT = Path(__file__).resolve().parents[2]
SESSION_FILE = ROOT / ".qa" / "session.json"
ENV_FILE = ROOT / ".env"

#: A request that takes longer than this against a local, seeded deployment is
#: not slow, it is wedged — a pool that never connected, a query fanning out to
#: every shard. Generous on purpose: this is a liveness bound, not a benchmark,
#: and `packages/cb-core/bench/` is where performance is measured.
BUDGET_SECONDS = 5.0


def _dotenv(key: str) -> str | None:
    if not ENV_FILE.exists():
        return None
    for line in ENV_FILE.read_text().splitlines():
        name, _, value = line.strip().partition("=")
        if name.strip() == key and not line.lstrip().startswith("#"):
            return value.strip().strip("'\"")
    return None


def _bot_token() -> str | None:
    """The token the *running server* verifies against, which is the one in
    `.env` — not the one `qa/conftest.py` put in this process's environment."""
    raw = _dotenv("CB_BOT_TOKENS")
    if not raw:
        return None
    try:
        tokens = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return str(next(iter(tokens.values()))) if tokens else None


@dataclass(frozen=True)
class Deployment:
    """A running API, and the seeded identities to talk to it as."""

    url: str
    group_id: int
    owner: str
    admin: str
    stranger: str


@pytest.fixture(scope="session")
def live() -> Iterator[Deployment]:
    if not SESSION_FILE.exists():
        pytest.skip("no seeded deployment — run `uv run scripts/qa_setup.py`")
    session = json.loads(SESSION_FILE.read_text())
    url = os.environ.get("CB_QA_API_URL", session.get("api_url", "http://localhost:8000"))

    try:
        httpx.get(f"{url}/healthz", timeout=2.0).raise_for_status()
    except httpx.HTTPError as exc:
        pytest.skip(f"nothing answering at {url} ({exc}) — run `uv run scripts/qa_setup.py`")

    token = _bot_token()
    if token is None:
        pytest.skip("no CB_BOT_TOKENS in .env — run `uv run scripts/qa_setup.py doctor`")

    users = session["users"]

    def mint(user_id: int) -> str:
        response = httpx.post(
            f"{url}/oauth2/token",
            json={"grant_type": MINIAPP_GRANT, "init_data": init_data(user_id, token)},
            timeout=10.0,
        )
        if response.status_code != 200:
            pytest.skip(f"the running API refused a freshly signed session: {response.text}")
        return str(response.json()["access_token"])

    yield Deployment(
        url=url,
        group_id=int(session["group_ids"][0]),
        owner=mint(int(users["owner"])),
        admin=mint(int(users["admin"])),
        stranger=mint(int(users["stranger"])),
    )


@pytest.fixture(scope="session")
def client(live: Deployment) -> Iterator[httpx.Client]:
    with httpx.Client(base_url=live.url, timeout=BUDGET_SECONDS + 5) as http:
        yield http


def call(client: httpx.Client, path: str, token: str | None = None) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    started = time.perf_counter()
    response = client.get(path, headers=headers)
    elapsed = time.perf_counter() - started
    assert elapsed < BUDGET_SECONDS, f"GET {path} took {elapsed:.1f}s — the deployment is wedged"
    return response


# ------------------------------------------------------------------ is it up


def test_the_process_is_up(client: httpx.Client) -> None:
    body = call(client, "/healthz").json()

    assert body["status"] == "ok"
    assert body["service"] == "cb-api"


def test_its_dependencies_are_reachable(client: httpx.Client) -> None:
    """The one check that separates "the process started" from "it can serve".
    A 503 here means Postgres or Valkey is not answering *from the server's
    point of view*, which is a different fact from this test host reaching
    them."""
    response = call(client, "/readyz")

    assert response.status_code == 200, response.text
    assert response.json() == {"ready": True, "postgres": True, "valkey": True}


def test_the_published_documents_are_served(client: httpx.Client) -> None:
    """A client generator and every token verifier start here."""
    assert call(client, "/openapi.json").status_code == 200
    assert "keys" in call(client, "/.well-known/jwks.json").json()
    discovery = call(client, "/.well-known/openid-configuration").json()
    assert discovery["token_endpoint"].endswith("/oauth2/token")


# ------------------------------------------------------------- is it wired up


def test_a_signed_session_reaches_the_group_it_administers(
    client: httpx.Client, live: Deployment
) -> None:
    """End to end through a socket: a payload this process signed, a token the
    server issued against its own keys, and a row from its own database."""
    me = call(client, "/me", live.admin).json()

    assert live.group_id in [group["group_id"] for group in me["groups"]]


@pytest.mark.parametrize(
    ("name", "path"),
    [
        ("config", "/groups/{group}/config"),
        ("rules", "/groups/{group}/rules"),
        ("audit", "/groups/{group}/audit"),
        ("summary", "/groups/{group}/analytics/summary"),
        ("commands", "/groups/{group}/analytics/commands"),
    ],
    ids=lambda value: value if "/" not in str(value) else "",
)
def test_each_group_surface_answers(
    client: httpx.Client, live: Deployment, name: str, path: str
) -> None:
    response = call(client, path.format(group=live.group_id), live.admin)

    assert response.status_code == 200, f"{name}: {response.text}"
    assert response.headers["content-type"].startswith("application/json")


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
def test_each_fleet_surface_answers_an_owner(
    client: httpx.Client, live: Deployment, path: str
) -> None:
    assert call(client, path, live.owner).status_code == 200


def test_the_deployment_knows_about_the_seeded_groups(
    client: httpx.Client, live: Deployment
) -> None:
    overview = call(client, "/admin/overview", live.owner).json()

    assert overview["reach"]["groups"] >= 1
    assert overview["reach"]["members"] >= 1


# ------------------------------------------------- is the boundary still there


def test_an_unauthenticated_request_is_challenged(client: httpx.Client) -> None:
    """Through the socket, so the header survived the middleware stack — which
    is the part an in-process test cannot vouch for."""
    response = call(client, "/me")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_a_stranger_cannot_see_a_group(client: httpx.Client, live: Deployment) -> None:
    assert call(client, f"/groups/{live.group_id}/config", live.stranger).status_code == 404


def test_a_group_admin_cannot_see_the_fleet(client: httpx.Client, live: Deployment) -> None:
    assert call(client, "/admin/overview", live.admin).status_code == 403


def test_the_interactive_docs_are_local_only_or_absent(client: httpx.Client) -> None:
    """`/docs` executes requests, so a production deployment does not hand it
    out (D12). Locally it is served; either answer is correct, and a 500 is
    not."""
    assert call(client, "/docs").status_code in {200, 404}
