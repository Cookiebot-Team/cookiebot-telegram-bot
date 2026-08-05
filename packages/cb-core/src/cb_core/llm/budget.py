"""Tenant monthly LLM spend cap.

`Tenant.monthly_llm_budget_usd` (`cb_core/tenancy.py:50`) has existed since
`packages/cb-api/migrations/versions/0003_tenants.py:40` and has never been read
by anything until this module. It is a hard cap: once a tenant's month-to-date
spend reaches it, `LLMRouter.complete()`/`transcribe()` refuse the call before
it reaches a provider (design `R2`).

Failure direction is not symmetric (`R2.4`). Over budget per a spend query that
*succeeded* raises `LLMBudgetExceededError` — that is the cap doing its job. A
cache or database *failure* while computing the spend is not evidence of
overspend, so it fails open: the call is allowed, the failure is logged and
counted. Same precedent as `cb_gateway/handlers/stickerspam.py`'s `_bump`,
which fails open when Valkey is unreachable rather than blocking every message.

`_query_month_to_date_usd` filters only on `tenant_id` (see its own docstring
for why that is unavoidable), which makes it a cross-shard scatter-gather
rather than a router query — AGENTS.md §4 rule 1's "every hot query filters on
`group_id`" does not hold for it. `month_to_date_usd` is what keeps that off
the reply path: it serves the cached total immediately and only ever queries
synchronously on a genuinely empty cache (first call for a tenant), refreshing
a stale-but-present value in the background instead of blocking the caller on
it. `tenant_monthly_cost` (`packages/cb-api/migrations/versions/0003_tenants.py:78-92`)
already exists for exactly this per-tenant rollup and is currently unpopulated
— a worker job (`cb_worker`) writing into it on the same cadence as
`rollup_llm_costs` is the proper long-term fix (a single-shard read instead of
a fan-out), but that worker is out of scope here; this module only gets the
existing fan-out off the reply path.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime

from cb_core import cache, db, metrics, tenancy
from cb_core.llm.types import LLMBudgetExceededError
from cb_core.logging import get_logger

log = get_logger("cb.llm.budget")

# A cached total younger than this is served as-is, no query at all. Older is
# still served (never blocks the caller) but triggers a background refresh —
# see `month_to_date_usd`.
_STALE_AFTER_SECONDS = 60
# Safety-net TTL on the cache entry itself: if nothing ever refreshes it (a
# crashed background task, say), Valkey reclaims it rather than serving an
# arbitrarily old total forever. Generous relative to `_STALE_AFTER_SECONDS`
# because a refresh is attempted long before this fires — it is a backstop,
# not the freshness mechanism.
_CACHE_TTL_SECONDS = 3600

# One in-flight background refresh per tenant at a time, so a burst of stale
# reads for the same tenant does not each fan out its own cross-shard query.
_refreshing: set[str] = set()


def _cache_key(tenant_id: str) -> str:
    return f"cb:llm:mtd:{tenant_id}"


def _now() -> float:
    # Wall clock, not `time.monotonic()`: the cached `computed_at` is read
    # back by a process (or replica) that did not write it, sometimes after a
    # restart, and only wall-clock timestamps are comparable across that gap.
    return time.time()


async def month_to_date_usd(tenant_id: str) -> float:
    """Month-to-date USD spend for `tenant_id`, UTC calendar month.

    Serves the cached total and refreshes it in the background when stale,
    rather than blocking a user's reply on `_query_month_to_date_usd`'s
    cross-shard aggregate — AGENTS.md §4 rule 4, "nothing slow on the reply
    path", and this call sits directly on it (`LLMRouter.complete()`/
    `transcribe()`).

    The one exception is the very first call for a tenant: with nothing
    cached yet, there is no stale value to serve, so this blocks on the query
    once. That is a deliberate choice over failing open on a cold cache
    (returning $0 spent and letting the call through): unlike the `Exception`
    path in `ensure_within_budget`, an empty cache is not an infrastructure
    failure, it is "not computed yet", and failing open on it would let an
    unbounded burst of a brand-new (or cache-flushed) tenant's very first
    messages all read "$0 spent" before the cache is ever populated — for a
    *hard* cap, that is a real gap, not a conservative default. Blocking once
    per tenant (then O(1) cache reads for the `_CACHE_TTL_SECONDS` after) is
    the trade the design accepts instead.
    """
    key = _cache_key(tenant_id)
    cached = await cache.get_json(key)
    if cached is not None:
        total = float(cached["total"])
        if _now() - float(cached["computed_at"]) >= _STALE_AFTER_SECONDS:
            _refresh_in_background(tenant_id)
        return total

    total = await _query_month_to_date_usd(tenant_id)
    await _store(tenant_id, total)
    return total


def _refresh_in_background(tenant_id: str) -> asyncio.Task[None] | None:
    """Fire-and-forget refresh of `tenant_id`'s cached total.

    Returns the scheduled task (mainly so tests can await it deterministically
    instead of racing the event loop) or `None` when a refresh for this
    tenant is already in flight.
    """
    if tenant_id in _refreshing:
        return None
    _refreshing.add(tenant_id)

    async def _run() -> None:
        try:
            total = await _query_month_to_date_usd(tenant_id)
            await _store(tenant_id, total)
        except Exception as exc:  # noqa: BLE001 - a failed background refresh must never surface anywhere; the stale value already handed to the caller stands until the next successful refresh
            log.warning("llm.budget_refresh_failed", tenant_id=tenant_id, error=str(exc))
        finally:
            _refreshing.discard(tenant_id)

    return asyncio.ensure_future(_run())


async def _store(tenant_id: str, total: float) -> None:
    try:
        await cache.set_json(
            _cache_key(tenant_id),
            {"total": total, "computed_at": _now()},
            ttl_seconds=_CACHE_TTL_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 - a cache write failure must not lose a total we already computed
        log.warning("llm.budget_cache_write_failed", tenant_id=tenant_id, error=str(exc))


# Module constants, not inlined in `_query_month_to_date_usd`, so
# `qa/integration/test_citus_topology.py` can EXPLAIN the exact SQL this
# module issues instead of retyping it — a copy in the test would silently
# drift from the real query the first time either one changed.
_ROLLED_UP_SQL = """
    SELECT coalesce(sum(d.cost_usd), 0) AS total
    FROM llm_daily_cost d
    JOIN groups g ON g.group_id = d.group_id
    WHERE g.tenant_id = $1 AND d.day >= $2 AND d.day < $3
    """
_TODAY_LIVE_SQL = """
    SELECT coalesce(sum(u.cost_usd), 0) AS total
    FROM llm_usage u
    JOIN groups g ON g.group_id = u.group_id
    WHERE g.tenant_id = $1 AND u.created_at >= $2
    """


async def _query_month_to_date_usd(tenant_id: str) -> float:
    """The nightly `llm_daily_cost` rollup for the month so far, plus today's
    `llm_usage` rows the rollup has not folded in yet (R2.3, `cb_worker/main.py`'s
    `rollup_llm_costs`).

    `llm_daily_cost` and `llm_usage` are both distributed on `group_id`, colocated
    with `groups` (`0002_media_and_llm_usage.py`); `groups.tenant_id`
    (`0003_tenants.py:71`) is the join key from a shard-local `group_id` to a
    tenant. Filtering only on `tenant_id`, with no `group_id` predicate, is a
    cross-shard aggregate rather than a router query — unavoidable for a
    *tenant*-scoped total over a *group*-sharded table. `month_to_date_usd` is
    what keeps this off the reply path (see its own docstring); this function
    is never called synchronously except on a tenant's first-ever check.
    """
    now = datetime.now(UTC)
    month_start = now.date().replace(day=1)
    today = now.date()
    today_start = datetime(today.year, today.month, today.day, tzinfo=UTC)

    rolled_up = await db.fetchrow(
        _ROLLED_UP_SQL,
        tenant_id,
        month_start,
        today,
        name="llm_budget_rolled_up",
    )
    today_live = await db.fetchrow(
        _TODAY_LIVE_SQL,
        tenant_id,
        today_start,
        name="llm_budget_today",
    )
    rolled_total = float(rolled_up["total"]) if rolled_up is not None else 0.0
    today_total = float(today_live["total"]) if today_live is not None else 0.0
    return rolled_total + today_total


async def ensure_within_budget(tenant_id: str) -> None:
    """Raise `LLMBudgetExceededError` if `tenant_id` is over its monthly cap.

    A tenant with no `monthly_llm_budget_usd` configured is never checked — no
    cache read, no query (R2.5). On a cache or database failure while computing
    the spend, this allows the call through instead of raising: an
    infrastructure outage is not evidence of overspend (R2.4).
    """
    tenant = await tenancy.registry.by_id(tenant_id)
    if tenant.monthly_llm_budget_usd is None:
        return

    try:
        spent = await month_to_date_usd(tenant_id)
    except Exception as exc:  # noqa: BLE001 - R2.4: infra failure fails open, never closed
        log.warning("llm.budget_check_failed", tenant_id=tenant_id, error=str(exc))
        metrics.llm_budget_check_failed_total.inc()
        return

    if spent >= tenant.monthly_llm_budget_usd:
        raise LLMBudgetExceededError(tenant_id, spent, tenant.monthly_llm_budget_usd)
