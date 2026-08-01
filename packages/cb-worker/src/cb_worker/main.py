"""cb-worker — everything that must not run on the reply path.

M0 ships the harness plus the two scheduled jobs the schema needs:
partition maintenance and daily rollups. Media/AI/fan-out jobs land in M2-M3.

Replaces three separate v1 mechanisms:
  * recursive `threading.Timer` chains (`Publisher.py`, `Birthdays.py:57`)
  * a 5-minute `scheduler_check` loop on the primary bot process only
  * `time.sleep(0.4)`-per-group fan-out loops that blocked a handler thread
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any, ClassVar

from arq import cron
from arq.connections import RedisSettings
from opentelemetry.trace import SpanKind
from whenever import Instant

from cb_core import cache, db, metrics, storage
from cb_core.logging import configure_logging, get_logger
from cb_core.migrations import ensure_schema
from cb_core.settings import get_settings
from cb_core.telemetry import context_from_carrier, setup_tracing, span

settings = get_settings()
settings.service_name = "cb-worker"
configure_logging(settings)
setup_tracing(settings)
log = get_logger("cb.worker")


async def _job[T](name: str, ctx: dict[str, Any], fn: Callable[..., Awaitable[T]], *args: Any) -> T:
    """Shared wrapper: restores the caller's trace context, times, counts."""
    parent = context_from_carrier(ctx.get("trace_carrier"))
    start = time.perf_counter()
    outcome = "ok"
    token = None
    try:
        from opentelemetry import context as otel_context

        token = otel_context.attach(parent)
        with span(f"job.{name}", kind=SpanKind.CONSUMER):
            return await fn(*args)
    except Exception:
        outcome = "error"
        log.exception("job.failed", job=name)
        raise
    finally:
        if token is not None:
            from opentelemetry import context as otel_context

            otel_context.detach(token)
        metrics.job_duration.labels(job=name, outcome=outcome).observe(time.perf_counter() - start)


# --------------------------------------------------------------------- scheduled jobs


async def maintain_partitions(ctx: dict[str, Any]) -> int:
    """Keep 7 days of message_events partitions ahead; compress anything older than 7d.

    Runs hourly so a missed run is harmless — the function is idempotent.
    """

    async def run() -> int:
        created = await db.fetchrow(
            "SELECT cb_maintain_partitions($1, $2) AS created", 7, 7, name="maintain_partitions"
        )
        n = int(created["created"]) if created else 0
        log.info("partitions.maintained", created=n)
        return n

    return await _job("maintain_partitions", ctx, run)


async def rollup_yesterday(ctx: dict[str, Any]) -> None:
    """Recompute the last two days of rollups.

    Two days, not one, so late-arriving events near midnight are picked up; the
    rollup is an idempotent upsert.
    """

    async def run() -> None:
        today = Instant.now().to_system_tz().date()
        for offset in (1, 0):
            day = today.add(days=-offset)
            await db.execute("SELECT cb_rollup_day($1)", day.to_stdlib(), name="rollup_day")
            log.info("rollup.done", day=str(day))

    await _job("rollup_yesterday", ctx, run)


async def rollup_llm_costs(ctx: dict[str, Any]) -> None:
    """Aggregate `llm_usage` into `llm_daily_cost`.

    The GROUP BY carries `group_id`, which is the distribution column, so each
    shard aggregates locally and only the small result set crosses the network.
    """

    async def run() -> None:
        today = Instant.now().to_system_tz().date()
        for offset in (1, 0):
            day = today.add(days=-offset)
            await db.execute("SELECT cb_rollup_llm_day($1)", day.to_stdlib(), name="rollup_llm_day")
            log.info("llm.rollup.done", day=str(day))

    await _job("rollup_llm_costs", ctx, run)


async def collect_media_garbage(ctx: dict[str, Any]) -> int:
    """Delete blobs no group references any more.

    Deliberately off the reply path: the anti-join scans every `media_objects`
    shard (node-local, because `media_blobs` is a reference table) and the
    deletes hit the object store.
    """

    async def run() -> int:
        deleted = await storage.media().collect_garbage(limit=500)
        log.info("media.gc.done", deleted=deleted)
        return deleted

    return await _job("collect_media_garbage", ctx, run)


async def expire_captchas(ctx: dict[str, Any]) -> None:
    """Sweep timed-out captcha challenges (core_groupguardian: fail -> cannot join).

    v1 rewrote a whole flat file on every check; this is one indexed DELETE.
    """

    async def run() -> None:
        result = await db.execute(
            "DELETE FROM captcha_challenges WHERE solved_at IS NULL AND expires_at < now()",
            name="expire_captchas",
        )
        log.info("captcha.expired", result=result)

    await _job("expire_captchas", ctx, run)


# ------------------------------------------------------------------------- lifecycle


async def startup(ctx: dict[str, Any]) -> None:
    await ensure_schema(settings)
    await db.init_pool(settings)
    await cache.init_cache(settings)
    await storage.init_storage(settings)
    from cb_core.cooldowns import COMPILED

    metrics.start_metrics_server(settings.metrics_port, "cb-worker", "0.1.0", COMPILED)
    log.info("worker.started", cython=COMPILED, storage=storage.store().scheme)


async def shutdown(ctx: dict[str, Any]) -> None:
    await storage.close_storage()
    await cache.close_cache()
    await db.close_pool()
    log.info("worker.stopped")


class WorkerSettings:
    redis_settings = RedisSettings.from_dsn(settings.redis_dsn)
    functions: ClassVar[list] = [
        maintain_partitions,
        rollup_yesterday,
        rollup_llm_costs,
        collect_media_garbage,
        expire_captchas,
    ]
    cron_jobs: ClassVar[list] = [
        cron(maintain_partitions, minute=5),  # hourly at :05
        cron(rollup_yesterday, hour=0, minute=20),  # daily, after midnight
        cron(rollup_llm_costs, hour=0, minute=25),
        cron(collect_media_garbage, hour=3, minute=40),  # off-peak
        cron(expire_captchas, minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}),
    ]
    on_startup = startup
    on_shutdown = shutdown
    max_jobs = 10
    job_timeout = 300
    keep_result = 3600
