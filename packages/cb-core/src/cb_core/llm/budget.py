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
"""

from __future__ import annotations

from datetime import UTC, datetime

from cb_core import cache, db, metrics, tenancy
from cb_core.llm.types import LLMBudgetExceededError
from cb_core.logging import get_logger

log = get_logger("cb.llm.budget")

_CACHE_TTL_SECONDS = 60


def _cache_key(tenant_id: str) -> str:
    return f"cb:llm:mtd:{tenant_id}"


async def month_to_date_usd(tenant_id: str) -> float:
    """Month-to-date USD spend for `tenant_id`, UTC calendar month.

    Cached under `cb:llm:mtd:{tenant_id}` for `_CACHE_TTL_SECONDS` (R2.3), so a
    chatty group costs one aggregate query a minute rather than one per message.

    Raises whatever the cache read or the database raise. `ensure_within_budget`
    is the caller that turns a raise here into R2.4's fail-open behaviour; this
    function only reports what it can compute, not what to do when it can't.
    """
    key = _cache_key(tenant_id)
    cached = await cache.get_json(key)
    if cached is not None:
        return float(cached)

    total = await _query_month_to_date_usd(tenant_id)

    try:
        await cache.set_json(key, total, ttl_seconds=_CACHE_TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001 - a cache write failure must not lose a total we already computed
        log.warning("llm.budget_cache_write_failed", tenant_id=tenant_id, error=str(exc))

    return total


async def _query_month_to_date_usd(tenant_id: str) -> float:
    """The nightly `llm_daily_cost` rollup for the month so far, plus today's
    `llm_usage` rows the rollup has not folded in yet (R2.3, `cb_worker/main.py`'s
    `rollup_llm_costs`).

    `llm_daily_cost` and `llm_usage` are both distributed on `group_id`, colocated
    with `groups` (`0002_media_and_llm_usage.py`); `groups.tenant_id`
    (`0003_tenants.py:71`) is the join key from a shard-local `group_id` to a
    tenant. Filtering only on `tenant_id`, with no `group_id` predicate, is a
    cross-shard aggregate rather than a router query — expected here, since it is
    cached for a minute rather than run per message.
    """
    now = datetime.now(UTC)
    month_start = now.date().replace(day=1)
    today = now.date()
    today_start = datetime(today.year, today.month, today.day, tzinfo=UTC)

    rolled_up = await db.fetchrow(
        """
        SELECT coalesce(sum(d.cost_usd), 0) AS total
        FROM llm_daily_cost d
        JOIN groups g ON g.group_id = d.group_id
        WHERE g.tenant_id = $1 AND d.day >= $2 AND d.day < $3
        """,
        tenant_id,
        month_start,
        today,
        name="llm_budget_rolled_up",
    )
    today_live = await db.fetchrow(
        """
        SELECT coalesce(sum(u.cost_usd), 0) AS total
        FROM llm_usage u
        JOIN groups g ON g.group_id = u.group_id
        WHERE g.tenant_id = $1 AND u.created_at >= $2
        """,
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
