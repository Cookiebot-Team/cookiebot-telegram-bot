"""Reads over the daily rollup tables — the query half of `x_analytics_api`.

The rollups themselves have existed since migration `0001`
(`group_daily_stats`, `command_daily_stats`) and `0002` (`llm_daily_cost`), and
`cb-worker` has been filling them nightly (`cb_rollup_day`,
`cb_rollup_llm_day`). Nothing read them: the numbers were in Grafana by way of
Postgres, and there was no HTTP surface. This module is that surface's data
layer, in `cb-core` rather than `cb-api` because a rollup read is not
HTTP-shaped — `cb-worker`'s own reports and any future console want the same
rows.

## Every query filters on `group_id`, and says so in the WHERE

That is AGENTS.md §4's first rule, and here it is also the whole authorisation
model: an endpoint that could be asked for "every group's numbers" would be
both a cross-tenant leak and a fan-out to every shard. There is deliberately
no "all groups" function in this module. The rollup tables are distributed on
`group_id` and colocated with `groups`, so each of these is a single-shard
router query — `qa/integration/test_citus_topology.py` asserts `Task Count: 1`
for the ones that matter.

## Windows are bounded by the caller, not by a LIMIT here

A date range is the natural bound for a daily rollup, and it is what the index
(`PRIMARY KEY (group_id, day)`) serves. `cb_api.routers.analytics` clamps the
range; this module takes the two dates it is given and trusts them, the same
way every other repository in `cb-core` trusts its arguments.

## Today is read live, from the raw events (`x_live_stats` R2)

The rollups lag: a group admin looking at "today" would see yesterday's numbers
until the worker's next pass. So when the window includes *today (UTC)*, that
one day is aggregated straight from `message_events` / `llm_usage` with the
**same expressions as `cb_rollup_day` / `cb_rollup_llm_day`** (migrations `0001`
and `0002`), and the rollup tables are read only for days *before* today — a
row the 1-minute worker has already written for today is ignored, so nothing is
counted twice. Every live query still opens with `group_id = $1`: one shard,
`Task Count: 1`. "Today" is a parameter (`today=`, default `datetime.now(UTC)`)
so tests can pin it.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import msgspec

from cb_core import db


class DailyStats(msgspec.Struct, frozen=True):
    """One row of `group_daily_stats` — one group, one day."""

    day: date
    messages: int
    commands: int
    joins: int
    leaves: int
    captcha_issued: int
    captcha_solved: int
    active_users: int
    errors: int
    p95_latency_ms: int | None
    llm_tokens: int
    llm_cost_usd: float


class CommandStats(msgspec.Struct, frozen=True):
    """One command's totals across the requested window, not one row per day —
    "which commands does this group actually use" is the question, and a
    per-day breakdown of 40 commands is a chart nobody reads."""

    command: str
    invocations: int
    errors: int
    p95_latency_ms: int | None


class LlmCost(msgspec.Struct, frozen=True):
    """One provider/model's totals across the window."""

    provider: str
    model: str
    calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    refusals: int
    errors: int


_DAILY = """
SELECT day, messages, commands, joins, leaves, captcha_issued, captcha_solved,
       active_users, errors, p95_latency_ms, llm_tokens, llm_cost_usd
  FROM group_daily_stats
 WHERE group_id = $1
   AND day >= $2
   AND day <= $3
 ORDER BY day
"""

# max(p95) across the window, not avg(p95): averaging percentiles is wrong, and
# the useful summary of "how slow did this command get" is its worst day.
_COMMANDS = """
SELECT command,
       sum(invocations)::bigint AS invocations,
       sum(errors)::bigint      AS errors,
       max(p95_latency_ms)      AS p95_latency_ms
  FROM command_daily_stats
 WHERE group_id = $1
   AND day >= $2
   AND day <= $3
 GROUP BY command
 ORDER BY invocations DESC, command
"""

_LLM = """
SELECT provider, model,
       sum(calls)::bigint         AS calls,
       sum(input_tokens)::bigint  AS input_tokens,
       sum(output_tokens)::bigint AS output_tokens,
       sum(cost_usd)              AS cost_usd,
       sum(refusals)::bigint      AS refusals,
       sum(errors)::bigint        AS errors
  FROM llm_daily_cost
 WHERE group_id = $1
   AND day >= $2
   AND day <= $3
 GROUP BY provider, model
 ORDER BY cost_usd DESC, provider, model
"""


# Live-today variants. The aggregate expressions are copied from `cb_rollup_day` /
# `cb_rollup_llm_day` on purpose: the number a group sees live must equal the
# number the rollup later stores. `group_daily_stats.llm_*` is summed from
# `message_events.llm_*` (not `llm_usage`), so the live row does the same.
_LIVE_DAILY = """
SELECT count(*) FILTER (WHERE event_type = 'message')                     AS messages,
       count(*) FILTER (WHERE event_type = 'command')                     AS commands,
       count(*) FILTER (WHERE event_type = 'join')                        AS joins,
       count(*) FILTER (WHERE event_type = 'leave')                       AS leaves,
       count(*) FILTER (WHERE event_type = 'captcha' AND outcome = 'issued') AS captcha_issued,
       count(*) FILTER (WHERE event_type = 'captcha' AND outcome = 'solved') AS captcha_solved,
       count(DISTINCT user_id)                                            AS active_users,
       count(*) FILTER (WHERE outcome = 'error')                          AS errors,
       percentile_disc(0.95) WITHIN GROUP (ORDER BY latency_ms)           AS p95_latency_ms,
       coalesce(sum(llm_tokens), 0)                                       AS llm_tokens,
       coalesce(sum(llm_cost_usd), 0)                                     AS llm_cost_usd
  FROM message_events
 WHERE group_id = $1
   AND ts >= $2
   AND ts < $3
HAVING count(*) > 0
"""

_LIVE_COMMANDS = """
SELECT command,
       count(*)                                  AS invocations,
       count(*) FILTER (WHERE outcome = 'error') AS errors,
       percentile_disc(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95_latency_ms
  FROM message_events
 WHERE group_id = $1
   AND ts >= $2
   AND ts < $3
   AND command IS NOT NULL
 GROUP BY command
"""

_LIVE_LLM = """
SELECT provider, model,
       count(*)                                    AS calls,
       coalesce(sum(input_tokens), 0)::bigint      AS input_tokens,
       coalesce(sum(output_tokens), 0)::bigint     AS output_tokens,
       coalesce(sum(cost_usd), 0)                  AS cost_usd,
       count(*) FILTER (WHERE outcome = 'refusal') AS refusals,
       count(*) FILTER (WHERE outcome = 'error')   AS errors
  FROM llm_usage
 WHERE group_id = $1
   AND created_at >= $2
   AND created_at < $3
 GROUP BY provider, model
"""


def _today_utc(today: date | None) -> date:
    return today if today is not None else datetime.now(UTC).date()


def _live_bounds(today: date) -> tuple[datetime, datetime]:
    start = datetime(today.year, today.month, today.day, tzinfo=UTC)
    return start, start + timedelta(days=1)


def _split_window(start: date, end: date, today: date) -> tuple[date | None, bool]:
    """`(rollup_end, include_live)`: the last rolled-up day to read (None when the
    window has no day before today) and whether today falls inside the window."""
    rollup_end = min(end, today - timedelta(days=1))
    return (rollup_end if rollup_end >= start else None), start <= today <= end


async def daily(
    group_id: int, start: date, end: date, *, today: date | None = None
) -> tuple[DailyStats, ...]:
    """Every day in `[start, end]` that has data, oldest first.

    Days before today come from the rollup; today (UTC, `today=` to override)
    is computed live from `message_events`, so it is current to the second.

    Days with no activity have no row — `cb_rollup_day` writes only what it
    saw — so a caller drawing a chart fills gaps itself rather than this
    inventing zero rows it cannot distinguish from real ones.
    """
    today = _today_utc(today)
    rollup_end, include_live = _split_window(start, end, today)
    result: list[DailyStats] = []
    if rollup_end is not None:
        rows = await db.fetch(_DAILY, group_id, start, rollup_end, name="analytics_daily")
        result.extend(_daily_stats(row["day"], row) for row in rows)
    if include_live:
        live = await db.fetchrow(
            _LIVE_DAILY, group_id, *_live_bounds(today), name="analytics_daily_live"
        )
        if live is not None:
            result.append(_daily_stats(today, live))
    return tuple(result)


def _daily_stats(day: date, row: Any) -> DailyStats:
    return DailyStats(
        day=day,
        messages=row["messages"],
        commands=row["commands"],
        joins=row["joins"],
        leaves=row["leaves"],
        captcha_issued=row["captcha_issued"],
        captcha_solved=row["captcha_solved"],
        active_users=row["active_users"],
        errors=row["errors"],
        p95_latency_ms=row["p95_latency_ms"],
        llm_tokens=row["llm_tokens"],
        llm_cost_usd=float(row["llm_cost_usd"]),
    )


async def commands(
    group_id: int,
    start: date,
    end: date,
    *,
    limit: int = 20,
    today: date | None = None,
) -> tuple[CommandStats, ...]:
    """The most-used commands in the window, busiest first (today included live)."""
    today = _today_utc(today)
    rollup_end, include_live = _split_window(start, end, today)
    rows: list[Any] = []
    if rollup_end is not None:
        rows.extend(
            await db.fetch(_COMMANDS, group_id, start, rollup_end, name="analytics_commands")
        )
    if include_live:
        rows.extend(
            await db.fetch(
                _LIVE_COMMANDS, group_id, *_live_bounds(today), name="analytics_commands_live"
            )
        )
    merged: dict[str, CommandStats] = {}
    for row in rows:
        prior = merged.get(row["command"])
        merged[row["command"]] = CommandStats(
            command=row["command"],
            invocations=row["invocations"] + (prior.invocations if prior else 0),
            errors=row["errors"] + (prior.errors if prior else 0),
            p95_latency_ms=_max_or_none(
                row["p95_latency_ms"], prior.p95_latency_ms if prior else None
            ),
        )
    ordered = sorted(merged.values(), key=lambda c: (-c.invocations, c.command))
    return tuple(ordered[:limit])


def _max_or_none(a: int | None, b: int | None) -> int | None:
    present = [value for value in (a, b) if value is not None]
    return max(present) if present else None


async def llm_costs(
    group_id: int, start: date, end: date, *, today: date | None = None
) -> tuple[LlmCost, ...]:
    """Per provider/model spend in the window, most expensive first (today live)."""
    today = _today_utc(today)
    rollup_end, include_live = _split_window(start, end, today)
    rows: list[Any] = []
    if rollup_end is not None:
        rows.extend(await db.fetch(_LLM, group_id, start, rollup_end, name="analytics_llm"))
    if include_live:
        rows.extend(
            await db.fetch(_LIVE_LLM, group_id, *_live_bounds(today), name="analytics_llm_live")
        )
    merged: dict[tuple[str, str], LlmCost] = {}
    for row in rows:
        key = (row["provider"], row["model"])
        prior = merged.get(key)
        merged[key] = LlmCost(
            provider=row["provider"],
            model=row["model"],
            calls=row["calls"] + (prior.calls if prior else 0),
            input_tokens=row["input_tokens"] + (prior.input_tokens if prior else 0),
            output_tokens=row["output_tokens"] + (prior.output_tokens if prior else 0),
            cost_usd=float(row["cost_usd"]) + (prior.cost_usd if prior else 0.0),
            refusals=row["refusals"] + (prior.refusals if prior else 0),
            errors=row["errors"] + (prior.errors if prior else 0),
        )
    ordered = sorted(merged.values(), key=lambda c: (-c.cost_usd, c.provider, c.model))
    return tuple(ordered)


def summarise(rows: tuple[DailyStats, ...]) -> dict[str, float | int | None]:
    """Window totals from rows already fetched — no second query, and no
    `sum()` in SQL that would have to be kept in step with `DailyStats`.

    `p95_latency_ms` is the **worst** day's value, not an average of
    percentiles, for the reason `_COMMANDS` gives. `captcha_solve_rate` is
    `None` rather than `0.0` when no captcha was issued: "nobody was asked" and
    "nobody solved it" are different facts and a dashboard should not draw them
    the same.
    """
    issued = sum(row.captcha_issued for row in rows)
    latencies = [row.p95_latency_ms for row in rows if row.p95_latency_ms is not None]
    return {
        "days": len(rows),
        "messages": sum(row.messages for row in rows),
        "commands": sum(row.commands for row in rows),
        "joins": sum(row.joins for row in rows),
        "leaves": sum(row.leaves for row in rows),
        "errors": sum(row.errors for row in rows),
        "captcha_issued": issued,
        "captcha_solved": sum(row.captcha_solved for row in rows),
        "captcha_solve_rate": (
            sum(row.captcha_solved for row in rows) / issued if issued else None
        ),
        "peak_active_users": max((row.active_users for row in rows), default=0),
        "worst_p95_latency_ms": max(latencies, default=None),
        "llm_tokens": sum(row.llm_tokens for row in rows),
        "llm_cost_usd": round(sum(row.llm_cost_usd for row in rows), 4),
    }


__all__ = [
    "CommandStats",
    "DailyStats",
    "LlmCost",
    "commands",
    "daily",
    "llm_costs",
    "summarise",
]
