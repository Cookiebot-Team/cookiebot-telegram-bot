"""Gateway -> worker enqueue.

`cb-worker` has only ever registered cron jobs (`cb_worker/main.py`); nothing in
`cb-gateway` could hand it a job, which is exactly what
`handlers/calladms.py` and `handlers/groupguardian.py` both say when they explain
why their fan-out isn't implemented yet. This module is that missing piece: one
lazily created `arq` pool, built from the same Redis/Valkey DSN
`cb_core.cache` already uses for the group-config pub/sub and the cooldown
store (`cb_core/settings.py:redis_dsn`) — no second URL, no second settings
mechanism (AGENTS.md §8).

`enqueue` must never be the reason a handler's reply fails: the user already
got their answer by the time this is called (AGENTS.md §4), so a broker outage
is logged and counted, not raised. Job names are the shared constants in
`cb_core.jobs`, never a literal typed at the call site, so gateway and worker
cannot drift apart on a rename.
"""

from __future__ import annotations

from typing import Any, cast

from arq.connections import ArqRedis, RedisSettings, create_pool
from prometheus_client import Counter

from cb_core.logging import get_logger
from cb_core.settings import get_settings

log = get_logger("cb.gateway.queue")

# `job` only, ever — a group or user id here would be the cardinality bomb
# AGENTS.md §7 warns about, and `outcome` is bounded to "ok"/"error".
enqueue_total = Counter("cb_gateway_enqueue_total", "Jobs handed to cb-worker", ["job", "outcome"])

_pool: ArqRedis | None = None


async def _get_pool() -> ArqRedis:
    global _pool
    if _pool is None:
        settings = get_settings()
        _pool = await create_pool(RedisSettings.from_dsn(settings.redis_dsn))
    return _pool


async def enqueue(job: str, *args: object, **kwargs: object) -> bool:
    """Hand `job` to `cb-worker`. Returns whether it was accepted.

    Never raises: a handler that has already replied to Telegram must not fail
    because the broker is down. `args`/`kwargs` become the worker function's
    own arguments (`arq`'s `enqueue_job` convention) — keep them small scalars,
    never a payload the worker should instead re-read from the database.
    """
    try:
        pool = await _get_pool()
        # arq's own signature reserves `_job_id`/`_queue_name`/etc. as typed
        # keyword-only params ahead of the job's free-form `**kwargs`; our
        # surface is deliberately job-agnostic (R2.2), so the cast tells mypy
        # what arq already accepts at runtime for any key that isn't one of those.
        await pool.enqueue_job(job, *args, **cast(dict[str, Any], kwargs))
    except Exception as exc:  # noqa: BLE001 - broker failure must never reach the handler
        log.warning("queue.enqueue", job=job, error=str(exc))
        enqueue_total.labels(job=job, outcome="error").inc()
        return False
    enqueue_total.labels(job=job, outcome="ok").inc()
    return True


async def close() -> None:
    """Torn down from the gateway's shutdown path, next to `cache.close_cache()`."""
    global _pool
    if _pool is not None:
        await _pool.aclose()
        _pool = None


__all__ = ["close", "enqueue", "enqueue_total"]
