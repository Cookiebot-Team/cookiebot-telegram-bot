"""asyncpg pool.

v1 opened a fresh `requests` call per backend hit with no pooling and no retry
(FEATURE-MAP §5). Here there is exactly one pool per process, sized from settings,
with timeouts and pool gauges wired to Prometheus.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
import orjson

from cb_core import metrics
from cb_core.logging import get_logger
from cb_core.settings import Settings

log = get_logger("cb.db")

_pool: asyncpg.Pool | None = None


#: Startup parameters for every pooled connection. Both exist for the same
#: 23-second query.
#:
#: Startup parameters, not `SET` in the pool's `init` hook, and that is not a
#: style choice: asyncpg resets a connection when it is *released*, with
#: `pg_advisory_unlock_all(); CLOSE ALL; UNLISTEN *; RESET ALL` — the span that
#: appears in every trace in this system. `RESET ALL` returns every GUC to the
#: value it had at startup, so anything `SET` in `init` survives exactly one
#: checkout and then silently reverts. Sent in the startup packet, these *are*
#: the value `RESET ALL` resets to.
#:
#: asyncpg introspects a type the first time a connection returns one it has not
#: cached (`tenants.owner_ids` is the first `text[]` most connections see). That
#: introspection is one recursive CTE over pg_type/pg_class/pg_attribute, and on
#: this database it took **23.5s** — long enough that `command_timeout` cancelled
#: it, so the query that triggered it failed with a bare `TimeoutError` and its
#: caller quietly fell back. In UAT that was every `/config` write: ~30ms of
#: actual work, then 10s of waiting for a catalog query nobody wrote.
#:
#: Measured on the UAT cluster, same query, same connection:
#:
#:     baseline                                23_500 ms
#:     + citus.override_table_visibility=off    4_308 ms
#:     + jit=off                                   19 ms
#:
#: `citus.override_table_visibility` is the big one. Citus hides shard tables
#: from clients by injecting `relation_is_a_known_shard(oid) IS NOT TRUE` into
#: every scan of `pg_class` — and the introspection query scans `pg_class` 485
#: times, so that C function ran 1.37 million times. Turning the override off
#: means shards are visible to *this session's catalog queries*, which is
#: irrelevant to an application that never lists tables and is exactly what the
#: GUC is for. It only exists when Citus does, hence the fallback in `init_pool`.
#:
#: `jit=off` is the rest. The plan is large enough to cross `jit_above_cost`, so
#: PostgreSQL spent ~4s compiling a query that returns five rows. This is an
#: OLTP workload of single-row lookups; JIT has nothing to win here.
_CITUS_SETTING = "citus.override_table_visibility"


def _server_settings(settings: Settings, *, with_citus: bool) -> dict[str, str]:
    base = {"application_name": settings.service_name, "jit": "off"}
    return {**base, _CITUS_SETTING: "off"} if with_citus else base


async def _init_connection(conn: asyncpg.Connection) -> None:
    # orjson everywhere: asyncpg's default json codec is stdlib json.
    await conn.set_type_codec(
        "jsonb",
        encoder=lambda v: orjson.dumps(v).decode(),
        decoder=orjson.loads,
        schema="pg_catalog",
    )


async def init_pool(settings: Settings) -> asyncpg.Pool:
    global _pool
    if _pool is not None:
        return _pool

    async def _create(*, with_citus: bool) -> asyncpg.Pool:
        return await asyncpg.create_pool(
            dsn=settings.pg_dsn,
            min_size=settings.pg_pool_min,
            max_size=settings.pg_pool_max,
            command_timeout=settings.pg_command_timeout,
            init=_init_connection,
            server_settings=_server_settings(settings, with_citus=with_citus),
        )

    try:
        _pool = await _create(with_citus=True)
    except asyncpg.PostgresError as exc:
        # A startup parameter the server does not recognise refuses the
        # connection outright, and `citus.override_table_visibility` only exists
        # where Citus is loaded. Plain PostgreSQL is a supported deployment (the
        # unit suite runs against one), so it gets a pool without that setting
        # rather than no pool at all — it has no shards to hide and therefore
        # nothing to gain from it.
        log.info("pg.citus_visibility_unsupported", error=str(exc))
        _pool = await _create(with_citus=False)

    log.info("pg.pool.ready", min=settings.pg_pool_min, max=settings.pg_pool_max)
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("pg pool not initialised; call init_pool() during startup")
    return _pool


def _observe_pool() -> None:
    if _pool is None:
        return
    size = _pool.get_size()
    metrics.db_pool_size.set(size)
    metrics.db_pool_in_use.set(size - _pool.get_idle_size())


@asynccontextmanager
async def acquire() -> AsyncIterator[asyncpg.Connection]:
    p = pool()
    async with p.acquire() as conn:
        _observe_pool()
        try:
            yield conn
        finally:
            _observe_pool()


@asynccontextmanager
async def transaction() -> AsyncIterator[asyncpg.Connection]:
    """v1 had zero @Transactional anywhere; multi-step writes raced (FEATURE-MAP D5/D6)."""
    async with acquire() as conn, conn.transaction():
        yield conn


async def fetch(stmt: str, *args: Any, name: str = "fetch") -> list[asyncpg.Record]:
    # `Any`: *args are bound SQL parameters - genuinely any bindable value.
    start = time.perf_counter()
    try:
        async with acquire() as conn:
            return await conn.fetch(stmt, *args)
    finally:
        metrics.db_query_duration.labels(stmt=name).observe(time.perf_counter() - start)


async def fetchrow(stmt: str, *args: Any, name: str = "fetchrow") -> asyncpg.Record | None:
    # `Any`: see fetch().
    start = time.perf_counter()
    try:
        async with acquire() as conn:
            return await conn.fetchrow(stmt, *args)
    finally:
        metrics.db_query_duration.labels(stmt=name).observe(time.perf_counter() - start)


async def execute(stmt: str, *args: Any, name: str = "execute") -> str:
    # `Any`: see fetch().
    start = time.perf_counter()
    try:
        async with acquire() as conn:
            return await conn.execute(stmt, *args)
    finally:
        metrics.db_query_duration.labels(stmt=name).observe(time.perf_counter() - start)


async def executemany(
    stmt: str, rows: Sequence[Sequence[Any]], *, name: str = "executemany"
) -> None:
    start = time.perf_counter()
    try:
        async with acquire() as conn:
            await conn.executemany(stmt, rows)
    finally:
        metrics.db_query_duration.labels(stmt=name).observe(time.perf_counter() - start)


async def healthcheck() -> bool:
    try:
        async with acquire() as conn:
            return await conn.fetchval("SELECT 1") == 1
    except Exception as exc:  # noqa: BLE001
        log.warning("pg.healthcheck.failed", error=str(exc))
        return False
