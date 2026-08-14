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
"""

from __future__ import annotations

from datetime import date

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
 LIMIT $4
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


async def daily(group_id: int, start: date, end: date) -> tuple[DailyStats, ...]:
    """Every rollup row in `[start, end]`, oldest first.

    Days with no activity have no row — `cb_rollup_day` writes only what it
    saw — so a caller drawing a chart fills gaps itself rather than this
    inventing zero rows it cannot distinguish from real ones.
    """
    rows = await db.fetch(_DAILY, group_id, start, end, name="analytics_daily")
    return tuple(
        DailyStats(
            day=row["day"],
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
        for row in rows
    )


async def commands(
    group_id: int, start: date, end: date, *, limit: int = 20
) -> tuple[CommandStats, ...]:
    """The most-used commands in the window, busiest first."""
    rows = await db.fetch(_COMMANDS, group_id, start, end, limit, name="analytics_commands")
    return tuple(
        CommandStats(
            command=row["command"],
            invocations=row["invocations"],
            errors=row["errors"],
            p95_latency_ms=row["p95_latency_ms"],
        )
        for row in rows
    )


async def llm_costs(group_id: int, start: date, end: date) -> tuple[LlmCost, ...]:
    """Per provider/model spend in the window, most expensive first."""
    rows = await db.fetch(_LLM, group_id, start, end, name="analytics_llm")
    return tuple(
        LlmCost(
            provider=row["provider"],
            model=row["model"],
            calls=row["calls"],
            input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"],
            cost_usd=float(row["cost_usd"]),
            refusals=row["refusals"],
            errors=row["errors"],
        )
        for row in rows
    )


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
