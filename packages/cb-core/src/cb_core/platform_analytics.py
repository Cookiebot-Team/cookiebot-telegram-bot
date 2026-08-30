"""Fleet-wide reads over the same rollups `cb_core.analytics` reads per group.

`cb_core.analytics` says, in its own docstring, that there is deliberately no
"all groups" function in it. That rule is about the *group* surface: an
endpoint a group's admin can reach must never be able to ask for another
group's numbers, and every query behind it is a single-shard router query.

This module is the other surface, and it exists because "how is the bot doing"
is a real question with no answer today — the deployment's owners had Grafana
and nothing else, and a Mini App cannot open Grafana. Everything here is
aggregate-only and reachable **only** by a tenant owner
(`cb_api.security.bot_admin`); the group-scoped module is unchanged and is
still what an ordinary admin's requests go through.

## These queries fan out, on purpose, and that is the whole trade

Every statement below omits `group_id` from its `WHERE`, which AGENTS.md §4.1
forbids on the reply path. The justification is specific and does not
generalise:

* they run on the rollup tables, which hold one row per group per day — not per
  message — so a year of the largest deployment this codebase has is thousands
  of rows, not millions;
* every one of them aggregates *before* returning, so what crosses the network
  between the workers and the coordinator is the grouped result, not the rows;
* nothing on the reply path calls them. They are HTTP reads by a handful of
  owners, on a dashboard people open, not on a message the bot has to answer
  inside Telegram's timeout.

That is why they are here and not inlined into the router: a fan-out belongs
somewhere it can be named, commented and found again — never accidentally
copied into a handler. If a deployment ever grows enough groups for these to
hurt, the fix is a `platform_daily_stats` rollup written by `cb-worker`, not a
LIMIT bolted onto the query; the shapes below are already what such a rollup
would return.

## Windows, not LIMITs

Same rule as the per-group module: the caller bounds the window and this trusts
the two dates it is given. `cb_api.routers.admin` clamps them.
"""

from __future__ import annotations

from datetime import date, datetime

import msgspec

from cb_core import db


class PlatformDay(msgspec.Struct, frozen=True):
    """One day, summed across every group that had a row that day.

    `groups` is the count of those rows — how many groups were active that day,
    which is a different and more useful number than how many groups exist.
    """

    day: date
    groups: int
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


class GroupActivity(msgspec.Struct, frozen=True):
    """One group's totals across the window, for the "busiest groups" table.

    The title comes from `groups`, which is colocated on `group_id`, so the
    join is node-local even though the aggregation is not single-shard.
    """

    group_id: int
    title: str | None
    username: str | None
    messages: int
    commands: int
    errors: int
    peak_active_users: int
    llm_cost_usd: float


class PlatformCommand(msgspec.Struct, frozen=True):
    """One command across every group. `groups` is how many distinct groups
    used it — a command with 10,000 invocations in one group is a different
    fact from one used everywhere, and the totals alone cannot tell them
    apart."""

    command: str
    invocations: int
    errors: int
    groups: int
    p95_latency_ms: int | None


class PlatformLlmCost(msgspec.Struct, frozen=True):
    """One provider/model's spend across every group."""

    provider: str
    model: str
    calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    refusals: int
    errors: int


class GroupRow(msgspec.Struct, frozen=True):
    """A row of the group directory — what the bot is in, not what it did."""

    group_id: int
    title: str | None
    username: str | None
    chat_type: str
    skin: str
    joined_at: datetime
    left_at: datetime | None
    members: int
    admins: int


class Reach(msgspec.Struct, frozen=True):
    """How far the deployment reaches right now — a fact about the present, so
    it takes no window."""

    groups: int
    groups_left: int
    members: int
    admins: int


_PLATFORM_DAILY = """
SELECT day,
       count(*)::bigint             AS groups,
       sum(messages)::bigint        AS messages,
       sum(commands)::bigint        AS commands,
       sum(joins)::bigint           AS joins,
       sum(leaves)::bigint          AS leaves,
       sum(captcha_issued)::bigint  AS captcha_issued,
       sum(captcha_solved)::bigint  AS captcha_solved,
       sum(active_users)::bigint    AS active_users,
       sum(errors)::bigint          AS errors,
       max(p95_latency_ms)          AS p95_latency_ms,
       sum(llm_tokens)::bigint      AS llm_tokens,
       sum(llm_cost_usd)            AS llm_cost_usd
  FROM group_daily_stats
 WHERE day >= $1
   AND day <= $2
 GROUP BY day
 ORDER BY day
"""

# `active_users` is summed rather than deduplicated: the rollup counts distinct
# users *per group*, and one person in three groups is three actives here.
# Deduplicating across groups would need the raw events, which is a different
# and much more expensive question than the one this answers.

_TOP_GROUPS = """
SELECT s.group_id,
       g.title,
       g.username,
       sum(s.messages)::bigint     AS messages,
       sum(s.commands)::bigint     AS commands,
       sum(s.errors)::bigint       AS errors,
       max(s.active_users)::bigint AS peak_active_users,
       sum(s.llm_cost_usd)         AS llm_cost_usd
  FROM group_daily_stats s
  JOIN groups g ON g.group_id = s.group_id
 WHERE s.day >= $1
   AND s.day <= $2
 GROUP BY s.group_id, g.title, g.username
 ORDER BY messages DESC, s.group_id
 LIMIT $3
"""

_PLATFORM_COMMANDS = """
SELECT command,
       sum(invocations)::bigint        AS invocations,
       sum(errors)::bigint             AS errors,
       count(DISTINCT group_id)::bigint AS groups,
       max(p95_latency_ms)             AS p95_latency_ms
  FROM command_daily_stats
 WHERE day >= $1
   AND day <= $2
 GROUP BY command
 ORDER BY invocations DESC, command
 LIMIT $3
"""

_PLATFORM_LLM = """
SELECT provider, model,
       sum(calls)::bigint         AS calls,
       sum(input_tokens)::bigint  AS input_tokens,
       sum(output_tokens)::bigint AS output_tokens,
       sum(cost_usd)              AS cost_usd,
       sum(refusals)::bigint      AS refusals,
       sum(errors)::bigint        AS errors
  FROM llm_daily_cost
 WHERE day >= $1
   AND day <= $2
 GROUP BY provider, model
 ORDER BY cost_usd DESC, provider, model
"""

# Keyset over `group_id`, not OFFSET (D11). The two subqueries are colocated
# per-group counts, so each one is answered on the shard that already holds the
# row — a LEFT JOIN with GROUP BY over three distributed tables would not be.
_DIRECTORY = """
SELECT g.group_id, g.title, g.username, g.chat_type, g.skin, g.joined_at, g.left_at,
       (SELECT count(*) FROM group_members m
         WHERE m.group_id = g.group_id AND m.left_at IS NULL)::bigint AS members,
       (SELECT count(*) FROM group_admins a
         WHERE a.group_id = g.group_id)::bigint                       AS admins
  FROM groups g
 WHERE ($1::bigint IS NULL OR g.group_id > $1)
   AND ($2::boolean IS FALSE OR g.left_at IS NULL)
   AND ($3::text IS NULL OR g.title ILIKE $3 OR g.username ILIKE $3)
 ORDER BY g.group_id
 LIMIT $4
"""

_REACH = """
SELECT (SELECT count(*) FROM groups WHERE left_at IS NULL)::bigint         AS groups,
       (SELECT count(*) FROM groups WHERE left_at IS NOT NULL)::bigint     AS groups_left,
       (SELECT count(*) FROM group_members WHERE left_at IS NULL)::bigint  AS members,
       (SELECT count(*) FROM group_admins)::bigint                         AS admins
"""


async def daily(start: date, end: date) -> tuple[PlatformDay, ...]:
    """Every day in `[start, end]` that any group was active, oldest first."""
    rows = await db.fetch(_PLATFORM_DAILY, start, end, name="platform_daily")
    return tuple(
        PlatformDay(
            day=row["day"],
            groups=row["groups"],
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
            llm_cost_usd=float(row["llm_cost_usd"] or 0),
        )
        for row in rows
    )


async def top_groups(start: date, end: date, *, limit: int = 20) -> tuple[GroupActivity, ...]:
    """The busiest groups in the window, most messages first."""
    rows = await db.fetch(_TOP_GROUPS, start, end, limit, name="platform_top_groups")
    return tuple(
        GroupActivity(
            group_id=row["group_id"],
            title=row["title"],
            username=row["username"],
            messages=row["messages"],
            commands=row["commands"],
            errors=row["errors"],
            peak_active_users=row["peak_active_users"],
            llm_cost_usd=float(row["llm_cost_usd"] or 0),
        )
        for row in rows
    )


async def commands(start: date, end: date, *, limit: int = 20) -> tuple[PlatformCommand, ...]:
    """Which commands the whole deployment uses, busiest first."""
    rows = await db.fetch(_PLATFORM_COMMANDS, start, end, limit, name="platform_commands")
    return tuple(
        PlatformCommand(
            command=row["command"],
            invocations=row["invocations"],
            errors=row["errors"],
            groups=row["groups"],
            p95_latency_ms=row["p95_latency_ms"],
        )
        for row in rows
    )


async def llm_costs(start: date, end: date) -> tuple[PlatformLlmCost, ...]:
    """What every group's AI features cost together, most expensive first."""
    rows = await db.fetch(_PLATFORM_LLM, start, end, name="platform_llm")
    return tuple(
        PlatformLlmCost(
            provider=row["provider"],
            model=row["model"],
            calls=row["calls"],
            input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"],
            cost_usd=float(row["cost_usd"] or 0),
            refusals=row["refusals"],
            errors=row["errors"],
        )
        for row in rows
    )


async def directory(
    *,
    limit: int = 50,
    after: int | None = None,
    search: str | None = None,
    active_only: bool = True,
) -> tuple[GroupRow, ...]:
    """The groups this deployment knows, ordered by `group_id`.

    Keyset-paginated on `group_id` rather than offset: chat ids are stable and
    unique, so "everything after this one" is a cursor that cannot skip or
    repeat a row when a group is added between two pages.
    """
    pattern = f"%{search}%" if search else None
    rows = await db.fetch(_DIRECTORY, after, active_only, pattern, limit, name="platform_directory")
    return tuple(
        GroupRow(
            group_id=row["group_id"],
            title=row["title"],
            username=row["username"],
            chat_type=row["chat_type"],
            skin=row["skin"],
            joined_at=row["joined_at"],
            left_at=row["left_at"],
            members=row["members"],
            admins=row["admins"],
        )
        for row in rows
    )


async def reach() -> Reach:
    """Groups, members and admins right now. Four counts in one round trip."""
    row = await db.fetchrow(_REACH, name="platform_reach")
    if row is None:  # pragma: no cover - a scalar aggregate always returns a row
        return Reach(groups=0, groups_left=0, members=0, admins=0)
    return Reach(
        groups=row["groups"],
        groups_left=row["groups_left"],
        members=row["members"],
        admins=row["admins"],
    )


def summarise(rows: tuple[PlatformDay, ...]) -> dict[str, float | int | None]:
    """Window totals, the same way `cb_core.analytics.summarise` does it.

    Not shared with that function: it takes `DailyStats`, these are
    `PlatformDay`, and the one number that differs is the one that matters —
    `peak_groups` is the busiest *day's* group count, which has no per-group
    equivalent. A common helper taking a protocol would hide that difference
    to save eight lines.
    """
    issued = sum(row.captcha_issued for row in rows)
    latencies = [row.p95_latency_ms for row in rows if row.p95_latency_ms is not None]
    return {
        "days": len(rows),
        "peak_groups": max((row.groups for row in rows), default=0),
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
    "GroupActivity",
    "GroupRow",
    "PlatformCommand",
    "PlatformDay",
    "PlatformLlmCost",
    "Reach",
    "commands",
    "daily",
    "directory",
    "llm_costs",
    "reach",
    "summarise",
    "top_groups",
]
