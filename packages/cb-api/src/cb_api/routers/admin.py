"""x_admin_api — the deployment's own numbers, for whoever runs it.

`x_analytics_api` answers "how is *my group* doing" and refuses to answer
anything wider — every query it makes carries a `group_id` and the caller must
administer that group. That is right for a group admin and useless to the
person who runs the bot: they cannot see how many groups it is in, which of
them are alive, what the whole fleet costs in LLM tokens, or which commands
anyone actually uses. Until now that answer lived only in Grafana, which is not
something a Mini App can open.

This router is that surface. Three things define it.

**The caller runs the deployment, not a group.** `cb_api.security.bot_admin_caller`
takes the tenant's `owner_ids` and `CB_OWNER_ID` — the same people the
owner-only Telegram commands answer to. Everyone else gets **403**, not the
404 the group endpoints answer with: `/admin/overview` is in the OpenAPI
document, so there is no chat id to hide, and pretending the path does not
exist would only confuse an owner holding the wrong token.

**`admin:read` is granted, never assumed.** A Mini App session's scopes are the
deployment's `CB_MINIAPP_SCOPES` for everybody — except this one, which
`routers/oauth.py` adds only when the subject really is an owner. So a stolen
non-owner token cannot reach these endpoints even if the thief edits their own
request, and the scope in the token is a true statement about who asked for it.

**Everything is aggregate.** Nothing here returns a message, a member's name or
a group's rules — those live behind the group endpoints, where a group's own
admins gate them. An owner who wants a specific group's settings calls
`/groups/{id}/config` like anybody else (and, being a tenant owner, is let
in by `security.administers`). The split is deliberate: "run the fleet" and
"read a member's data" are different powers and only one of them is here.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from cb_api.refusals import BAD_WINDOW, fleet_errors
from cb_api.routers.analytics import Window, resolve_window
from cb_api.security import Caller, bot_admin_caller
from cb_core import platform_analytics, tenancy
from cb_core.logging import get_logger
from cb_core.settings import get_settings

log = get_logger("cb.api.admin")

router = APIRouter(prefix="/admin", tags=["admin"])

#: Read is the only power this router has today. Written as a constant because
#: it appears on every route and a typo in one of them would be a hole rather
#: than an error.
_READ = "admin:read"

#: The refusals every endpoint here can answer with. No 404 — see the module
#: docstring, and `packages/cb-api/tests/test_openapi.py`, which asserts its
#: absence rather than trusting this line.
_ERRORS = fleet_errors(BAD_WINDOW)

#: The two the endpoints with no date window can answer with.
_NO_WINDOW = fleet_errors()

Admin = Annotated[Caller, Depends(bot_admin_caller(_READ))]


class ReachBody(BaseModel):
    """Where the bot is right now — no window, because this is the present."""

    groups: int = Field(description="groups the bot is currently in")
    groups_left: int = Field(description="groups it was removed from, kept for their history")
    members: int = Field(
        description="current memberships, summed — a person in three groups is three"
    )
    admins: int


class WindowedResponse(BaseModel):
    start: date
    end: date


class PlatformDayRow(BaseModel):
    """One day, summed across every group that was active on it."""

    day: date
    groups: int = Field(description="how many groups were active that day")
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


class PlatformDailyResponse(WindowedResponse):
    """The deployment's activity, one row per day."""

    days: list[PlatformDayRow]


class PlatformSummary(BaseModel):
    """The window's totals across every group, with the peaks called out
    rather than averaged — an average of daily percentiles means nothing."""

    days: int = Field(description="how many days in the window had any activity at all")
    peak_groups: int = Field(description="the busiest day's count of active groups")
    messages: int
    commands: int
    joins: int
    leaves: int
    errors: int
    captcha_issued: int
    captcha_solved: int
    captcha_solve_rate: float | None = Field(
        default=None, description="null when nobody was challenged anywhere"
    )
    peak_active_users: int
    worst_p95_latency_ms: int | None
    llm_tokens: int
    llm_cost_usd: float


class BudgetBody(BaseModel):
    """The tenant's soft LLM budget against what the window actually cost.

    `monthly_llm_budget_usd` is what `cb_core.llm` refuses `chat` past, so an
    owner watching this number is watching the thing that will silently turn
    the conversational features off.
    """

    monthly_llm_budget_usd: float | None = Field(
        default=None, description="null when the tenant has no budget configured"
    )
    spent_usd: float = Field(description="the requested window's spend, not the calendar month's")
    remaining_usd: float | None = Field(default=None, description="null when there is no budget")


class OverviewResponse(WindowedResponse):
    """One request for the dashboard's first screen.

    Three round trips server-side rather than three from the Mini App: a phone
    inside Telegram on a bad connection pays for each of them, and none of the
    three is useful without the others.
    """

    tenant_id: str
    display_name: str
    reach: ReachBody
    totals: PlatformSummary
    budget: BudgetBody


class GroupActivityRow(BaseModel):
    """One group's totals across the window, for the leaderboard."""

    group_id: int
    title: str | None
    username: str | None
    messages: int
    commands: int
    errors: int
    peak_active_users: int
    llm_cost_usd: float


class TopGroupsResponse(WindowedResponse):
    """The busiest groups. A leaderboard, not a directory: a group with no
    activity in the window is absent here and present in `/admin/groups`."""

    groups: list[GroupActivityRow]


class PlatformCommandRow(BaseModel):
    """One command across every group, with its reach as well as its volume."""

    command: str
    invocations: int
    errors: int
    groups: int = Field(description="distinct groups that used it — one busy group is not reach")
    p95_latency_ms: int | None


class PlatformCommandsResponse(WindowedResponse):
    """What the whole deployment's users actually type."""

    commands: list[PlatformCommandRow]


class PlatformLlmRow(BaseModel):
    """One provider/model's spend across every group."""

    provider: str
    model: str
    calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    refusals: int
    errors: int


class PlatformLlmResponse(WindowedResponse):
    """Fleet-wide AI spend, most expensive model first."""

    total_cost_usd: float
    models: list[PlatformLlmRow]


class DirectoryRow(BaseModel):
    """One group the bot knows — what it is in, not what it did."""

    group_id: int
    title: str | None
    username: str | None
    chat_type: str
    skin: str = Field(description="which bot persona serves it (`core_botskins`)")
    joined_at: datetime
    left_at: datetime | None = Field(
        default=None, description="set when the bot was removed; the row is kept for its history"
    )
    members: int
    admins: int


class DirectoryPage(BaseModel):
    """A page of the directory, ordered by `group_id`."""

    groups: list[DirectoryRow]
    next_after: int | None = Field(
        default=None,
        description="pass as `after` for the following page; null on the last one",
    )


class TenantResponse(BaseModel):
    """What this deployment is configured as. Read-only on purpose — see the
    note on the endpoint."""

    tenant_id: str
    display_name: str
    handler_pack: str
    default_locale: str
    disabled_commands: list[str]
    storage_prefix: str
    monthly_llm_budget_usd: float | None
    active: bool
    owner_ids: list[int] = Field(description="who else can reach this router")


@router.get(
    "/overview",
    summary="The whole deployment in one object: reach, the window's totals, the budget",
    response_model=OverviewResponse,
    responses=_ERRORS,
)
async def overview(_admin: Admin, start: Window = None, end: Window = None) -> dict[str, Any]:
    """Reach, the window's totals and the LLM budget, in one request."""
    window_start, window_end = resolve_window(start, end)
    tenant = await tenancy.registry.by_id(tenancy.DEFAULT_TENANT)
    reach = await platform_analytics.reach()
    rows = await platform_analytics.daily(window_start, window_end)
    totals = platform_analytics.summarise(rows)
    spent = float(totals["llm_cost_usd"] or 0)
    budget = tenant.monthly_llm_budget_usd
    return {
        "start": window_start,
        "end": window_end,
        "tenant_id": tenant.tenant_id,
        "display_name": tenant.display_name,
        "reach": {
            "groups": reach.groups,
            "groups_left": reach.groups_left,
            "members": reach.members,
            "admins": reach.admins,
        },
        "totals": totals,
        "budget": {
            "monthly_llm_budget_usd": budget,
            "spent_usd": spent,
            "remaining_usd": round(budget - spent, 4) if budget is not None else None,
        },
    }


@router.get(
    "/analytics/daily",
    summary="One row per day, summed across every group",
    response_model=PlatformDailyResponse,
    responses=_ERRORS,
)
async def daily(_admin: Admin, start: Window = None, end: Window = None) -> dict[str, Any]:
    """One row per day, summed across every group that was active that day.

    `active_users` is summed per group, so one person in three groups counts
    three times — deduplicating across groups would need the raw events rather
    than the rollups, which is a much more expensive question.
    """
    window_start, window_end = resolve_window(start, end)
    rows = await platform_analytics.daily(window_start, window_end)
    return {
        "start": window_start,
        "end": window_end,
        "days": [
            {
                "day": row.day,
                "groups": row.groups,
                "messages": row.messages,
                "commands": row.commands,
                "joins": row.joins,
                "leaves": row.leaves,
                "captcha_issued": row.captcha_issued,
                "captcha_solved": row.captcha_solved,
                "active_users": row.active_users,
                "errors": row.errors,
                "p95_latency_ms": row.p95_latency_ms,
                "llm_tokens": row.llm_tokens,
                "llm_cost_usd": row.llm_cost_usd,
            }
            for row in rows
        ],
    }


@router.get(
    "/analytics/groups",
    summary="The busiest groups in the window",
    response_model=TopGroupsResponse,
    responses=_ERRORS,
)
async def top_groups(
    _admin: Admin,
    start: Window = None,
    end: Window = None,
    limit: Annotated[int, Query(ge=1, le=100, description="how many groups to return")] = 20,
) -> dict[str, Any]:
    """The busiest groups in the window, most messages first.

    A leaderboard, not a directory: a group with no activity in the window is
    absent here and present in `/admin/groups`.
    """
    window_start, window_end = resolve_window(start, end)
    rows = await platform_analytics.top_groups(window_start, window_end, limit=limit)
    return {
        "start": window_start,
        "end": window_end,
        "groups": [
            {
                "group_id": row.group_id,
                "title": row.title,
                "username": row.username,
                "messages": row.messages,
                "commands": row.commands,
                "errors": row.errors,
                "peak_active_users": row.peak_active_users,
                "llm_cost_usd": row.llm_cost_usd,
            }
            for row in rows
        ],
    }


@router.get(
    "/analytics/commands",
    summary="Which commands the whole deployment uses",
    response_model=PlatformCommandsResponse,
    responses=_ERRORS,
)
async def commands(
    _admin: Admin,
    start: Window = None,
    end: Window = None,
    limit: Annotated[int, Query(ge=1, le=100, description="how many commands to return")] = 20,
) -> dict[str, Any]:
    """Which commands the deployment uses, busiest first, with how many groups
    each one reaches — a command with 10,000 invocations in one group and one
    used everywhere are different facts, and a decision to retire a command
    should not confuse them."""
    window_start, window_end = resolve_window(start, end)
    rows = await platform_analytics.commands(window_start, window_end, limit=limit)
    return {
        "start": window_start,
        "end": window_end,
        "commands": [
            {
                "command": row.command,
                "invocations": row.invocations,
                "errors": row.errors,
                "groups": row.groups,
                "p95_latency_ms": row.p95_latency_ms,
            }
            for row in rows
        ],
    }


@router.get(
    "/analytics/llm",
    summary="What every group's AI features cost together",
    response_model=PlatformLlmResponse,
    responses=_ERRORS,
)
async def llm(_admin: Admin, start: Window = None, end: Window = None) -> dict[str, Any]:
    """Fleet-wide LLM spend, per provider and model, most expensive first."""
    window_start, window_end = resolve_window(start, end)
    rows = await platform_analytics.llm_costs(window_start, window_end)
    return {
        "start": window_start,
        "end": window_end,
        "total_cost_usd": round(sum(row.cost_usd for row in rows), 4),
        "models": [
            {
                "provider": row.provider,
                "model": row.model,
                "calls": row.calls,
                "input_tokens": row.input_tokens,
                "output_tokens": row.output_tokens,
                "cost_usd": row.cost_usd,
                "refusals": row.refusals,
                "errors": row.errors,
            }
            for row in rows
        ],
    }


@router.get(
    "/groups",
    summary="Every group the bot knows, keyset-paginated",
    response_model=DirectoryPage,
    responses=_NO_WINDOW,
)
async def directory(
    _admin: Admin,
    limit: Annotated[int, Query(ge=1, le=200, description="groups per page")] = 50,
    after: Annotated[int | None, Query(description="last group_id of the previous page")] = None,
    search: Annotated[str | None, Query(max_length=64, description="title or @username")] = None,
    include_left: Annotated[
        bool, Query(description="also list groups the bot was removed from")
    ] = False,
) -> dict[str, Any]:
    """The directory, ordered by `group_id` and keyset-paginated (D11).

    The cursor is the id itself, which is stable and unique, so a group added
    between two pages cannot make a row repeat or vanish the way an OFFSET
    would.
    """
    rows = await platform_analytics.directory(
        limit=limit, after=after, search=search, active_only=not include_left
    )
    return {
        "groups": [
            {
                "group_id": row.group_id,
                "title": row.title,
                "username": row.username,
                "chat_type": row.chat_type,
                "skin": row.skin,
                "joined_at": row.joined_at,
                "left_at": row.left_at,
                "members": row.members,
                "admins": row.admins,
            }
            for row in rows
        ],
        "next_after": rows[-1].group_id if len(rows) == limit else None,
    }


@router.get(
    "/tenant",
    summary="How this deployment is configured",
    response_model=TenantResponse,
    responses=_NO_WINDOW,
)
async def tenant(_admin: Admin) -> dict[str, Any]:
    """The tenant's own row, read-only.

    Read-only because every field here changes how the *bot* behaves — which
    commands exist, which model answers, where media is written — and a Mini
    App form that could flip `active` or empty `owner_ids` would be one
    mis-tap from an outage nobody could undo through the same API. Editing a
    tenant is a deliberate database change, and it stays one.

    `bot_tokens` is deliberately absent: an owner who needs it has it already,
    and an endpoint that returns bot tokens is one stolen owner token away
    from being the whole deployment.
    """
    resolved = await tenancy.registry.by_id(tenancy.DEFAULT_TENANT)
    settings = get_settings()
    owners = set(resolved.owner_ids)
    if settings.owner_id:
        owners.add(settings.owner_id)
    return {
        "tenant_id": resolved.tenant_id,
        "display_name": resolved.display_name,
        "handler_pack": resolved.handler_pack,
        "default_locale": resolved.default_locale,
        "disabled_commands": sorted(resolved.disabled_commands),
        "storage_prefix": resolved.storage_prefix,
        "monthly_llm_budget_usd": resolved.monthly_llm_budget_usd,
        "active": resolved.active,
        "owner_ids": sorted(owners),
    }


__all__ = [
    "DirectoryPage",
    "OverviewResponse",
    "PlatformCommandsResponse",
    "PlatformDailyResponse",
    "PlatformLlmResponse",
    "TenantResponse",
    "TopGroupsResponse",
    "router",
]
