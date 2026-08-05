"""Integration-layer fixtures: a real Postgres/Citus, real rows, simulated users.

These tests skip cleanly when no database is reachable, so `python scripts/cb.py test` stays
offline-friendly and CI (which starts a Citus service) runs the full set.

They share the session event loop from `qa/conftest.py`, so `run(...)` drives
async code from synchronous test bodies — the same mechanism the BDD layer uses.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Coroutine, Iterator
from types import ModuleType
from typing import TYPE_CHECKING, Any

import pytest

from cb_core import db
from cb_core.settings import Settings

if TYPE_CHECKING:
    from qa.integration.factories import World

INTEGRATION_DSN = os.environ.get(
    "CB_TEST_PG_DSN",
    os.environ.get("CB_PG_DSN", "postgresql://cookiebot:cookiebot@localhost:5432/cookiebot"),
)

# Tables migration 0001/0002 must have created before these tests mean anything.
_REQUIRED_TABLES = (
    "groups",
    "users",
    "media_objects",
    "media_blobs",
    "llm_usage",
    "scheduled_posts",
)


@pytest.fixture(scope="session")
def pg(run: Callable[[Coroutine[Any, Any, Any]], Any]) -> Iterator[ModuleType]:
    """Connection pool, or skip the whole integration layer."""
    settings = Settings(
        pg_dsn=INTEGRATION_DSN,
        service_name="cb-tests",
        traces_enabled=False,
        # Not the 10s production default. The first statement carrying an array
        # parameter on a fresh connection makes asyncpg run its recursive
        # `typeinfo_tree` introspection, and this margin exists because that used
        # to take ~20s here.
        #
        # The cause was never the emulated container this comment used to blame:
        # Citus hides shards by injecting `relation_is_a_known_shard(oid)` into
        # every `pg_class` scan, the introspection query scans `pg_class` 485
        # times, and that call ran 1.37 million times. `cb_core/db.py` now opens
        # connections with `citus.override_table_visibility=off` and `jit=off`,
        # which took the same query from 23.5s to 19ms — in production, where it
        # was timing out mid-command, and here.
        #
        # The margin stays anyway: an integration suite that fails in teardown
        # because a first-connection cost drifted is a bad way to learn about it,
        # and `test_pool_session_settings.py` asserts the real bound.
        pg_command_timeout=float(os.environ.get("CB_TEST_PG_COMMAND_TIMEOUT", "60")),
    )
    try:
        run(db.init_pool(settings))
    except Exception as exc:  # noqa: BLE001 - any connection failure means "skip"
        pytest.skip(f"no database at {INTEGRATION_DSN}: {exc}")

    missing = run(_missing_tables())
    if missing:
        pytest.skip(
            f"schema not migrated, missing: {', '.join(missing)} (run `python scripts/cb.py migrate`)"
        )

    yield db
    run(db.close_pool())


async def _missing_tables() -> list[str]:
    rows = await db.fetch(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'", name="list_tables"
    )
    present = {r["tablename"] for r in rows}
    return [t for t in _REQUIRED_TABLES if t not in present]


@pytest.fixture
def world(pg: ModuleType, run: Callable[[Coroutine[Any, Any, Any]], Any]) -> Iterator[World]:
    """A disposable group with members — the 'simulated users' harness.

    Every test gets its own group id, so tests are order-independent and can run
    against a shared database without colliding.
    """
    from qa.integration.factories import World

    w = World(run)
    w.setup()
    yield w
    w.teardown()
