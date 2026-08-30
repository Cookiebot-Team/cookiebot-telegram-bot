"""Fixtures for the HTTP suite: an app, a seeded world, and three sessions.

Three decisions here are worth knowing before writing a test against them.

**The app is the real one, without its lifespan.** `cb_api.main.app` — every
router, the CORS middleware, the instrumentation — driven through
`httpx.ASGITransport`, so there is no port, no server and nothing to wait for.
Its lifespan is deliberately not entered: that would converge the schema, open a
second pool, connect Valkey and object storage, and bind the metrics port, none
of which is what these tests are about. The database pool comes from the `pg`
fixture instead, which is the same one every other integration test uses.

**Everything runs on one event loop.** `qa/conftest.py` owns it; `run()` drives
coroutines on it. The pool is created there, so the client has to live there
too — an `asyncpg` pool used from a second loop fails in a way that reads like a
mystery. `Api` (`qa/api/client.py`) is the wrapper that keeps that out of the
test bodies.

**Sessions are minted through the real token endpoint.** Not signed here and
handed to the client: the test posts `initData` to `/oauth2/token` exactly as a
Mini App would, and what comes back is verified against the keys in the
`signing_keys` table. So every test in this directory also exercises the token
path — and a break in it fails loudly here rather than silently downgrading
these tests to something weaker.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta
from typing import Any

import httpx
import pytest

from qa.api.auth import MINIAPP_GRANT, init_data
from qa.api.client import Api, Run, Tokens
from qa.integration import conftest as _integration
from qa.integration.factories import World

# The database fixtures are re-exported rather than redefined: "skip cleanly
# when no database is reachable" is one rule, and two copies of it drift into
# two different rules. pytest treats a fixture bound in a conftest as defined
# there, so this directory gets `pg`, `world` and `second_world` unchanged.
#
# Bound from the module rather than imported by name so that a fixture *using*
# one — `def api(pg, run)` — is not shadowing an import, which is a real ruff
# finding rather than a rule to silence.
pg = _integration.pg
world = _integration.world
second_world = _integration.second_world

#: The bot token `qa/conftest.py` puts in the environment. Every `initData` this
#: suite signs is signed with it, and the app verifies against the same value —
#: which is the whole trick that makes an HTTP suite possible with no Telegram.
BOT_TOKEN = "424242:TEST"

#: Ids well outside `factories.py`'s allocation, so a caller in this suite can
#: never collide with a member some other test created.
OWNER_ID = 880_000_001
STRANGER_ID = 880_000_002


@pytest.fixture(scope="session")
def api(pg: Any, run: Run) -> Iterator[Api]:
    """The real app, in process, on the suite's own event loop."""
    from cb_api.main import app

    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api.test")
    try:
        yield Api(client, run)
    finally:
        run(client.aclose())


@pytest.fixture
def owner(monkeypatch: pytest.MonkeyPatch) -> int:
    """A caller who runs the deployment, via `CB_OWNER_ID`.

    The setting, not the `tenants` row: it is the same source the owner-only
    Telegram commands read, it needs no cache invalidation, and
    `test_integration.py` covers the tenant path separately so both are proved.
    """
    from cb_core.settings import get_settings

    monkeypatch.setattr(get_settings(), "owner_id", OWNER_ID, raising=False)
    return OWNER_ID


@pytest.fixture
def group(world: World) -> World:
    """A disposable group with one admin, one plain member, and a month of
    rollups behind it.

    Its own group id per test, so the suite is order-independent and two tests
    that both write settings cannot see each other's writes.
    """
    world.add_user(admin=True)
    world.add_user()
    _seed_rollups(world)
    return world


def _seed_rollups(world: World) -> None:
    """Enough rows for the analytics endpoints to answer with something.

    Written directly rather than through `cb_rollup_day`: what these tests need
    is a known input, and recomputing one is `qa/integration/test_rollups.py`'s
    subject.
    """
    from cb_core import db

    today = date.today()
    for back in range(3):
        day = today - timedelta(days=back)
        world._run(  # noqa: SLF001 - `World` exposes its runner for exactly this
            db.execute(
                """
                INSERT INTO group_daily_stats
                    (group_id, day, messages, commands, active_users, captcha_issued,
                     captcha_solved, p95_latency_ms, llm_tokens, llm_cost_usd)
                VALUES ($1, $2, $3, 4, 3, 2, 1, 120, 500, 0.25)
                ON CONFLICT (group_id, day) DO NOTHING
                """,
                world.group_id,
                day,
                10 + back,
                name="qa_api_seed_daily",
            )
        )
        world._run(  # noqa: SLF001
            db.execute(
                """
                INSERT INTO command_daily_stats
                    (group_id, day, command, invocations, errors, p95_latency_ms)
                VALUES ($1, $2, 'dice', 5, 0, 40)
                ON CONFLICT (group_id, day, command) DO NOTHING
                """,
                world.group_id,
                day,
                name="qa_api_seed_command",
            )
        )
        world._run(  # noqa: SLF001
            db.execute(
                """
                INSERT INTO llm_daily_cost
                    (group_id, day, provider, model, calls, input_tokens, output_tokens,
                     cost_usd, refusals, errors)
                VALUES ($1, $2, 'anthropic', 'claude-opus-5', 3, 900, 300, 0.5, 0, 0)
                ON CONFLICT (group_id, day, provider, model) DO NOTHING
                """,
                world.group_id,
                day,
                name="qa_api_seed_llm",
            )
        )


def mint(api: Api, user_id: int) -> str:
    """An access token for `user_id`, through the endpoint a Mini App uses."""
    response = api.post(
        "/oauth2/token",
        json={"grant_type": MINIAPP_GRANT, "init_data": init_data(user_id, BOT_TOKEN)},
    )
    assert response.status_code == 200, f"token endpoint refused: {response.text}"
    return str(response.json()["access_token"])


@pytest.fixture
def tokens(api: Api, group: World, owner: int) -> Tokens:
    """One session per role, all three minted the way a client would.

    The admin is the group's creator; the stranger is in no group at all. That
    pairing is what makes the refusals testable, and the refusals are most of
    what this API does.
    """
    admin = next(user for user in group.users if user.is_admin)
    return Tokens(
        owner=mint(api, owner),
        admin=mint(api, admin.user_id),
        stranger=mint(api, STRANGER_ID),
    )
