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
    _pool = await asyncpg.create_pool(
        dsn=settings.pg_dsn,
        min_size=settings.pg_pool_min,
        max_size=settings.pg_pool_max,
        command_timeout=settings.pg_command_timeout,
        init=_init_connection,
        server_settings={"application_name": settings.service_name},
    )
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
