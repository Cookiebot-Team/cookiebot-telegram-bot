"""x_analytics_api — per-group numbers over HTTP.

The rollup tables have been filled nightly since M0 and read by nobody but
Grafana. These four endpoints are the surface a group's admins (and the web
console) get: what happened, which commands were used, what the LLM cost, and
one summary of all three.

Net-new — v1's Java backend had no analytics endpoint at all, and v1's bot had
no analytics. So there is no behaviour to be compatible with here, only this
codebase's own rules:

* **D11, no unbounded list.** Every endpoint takes a date window, defaults to
  30 days and refuses more than a year; the command list takes a `limit`
  capped at 100.
* **Every query filters on `group_id`** (AGENTS.md §4), which is also the
  authorisation boundary — see `cb_api.security.group_admin`.
* **A day is a `date`, not a timestamp.** The rollups are daily and computed in
  UTC by `cb_rollup_day`; an endpoint that accepted an instant would imply an
  hourly resolution that does not exist.
* **Every response is a declared model**, because the Mini App and the console
  are both built from `/openapi.json` and a chart drawn against `object` is a
  chart drawn against guesswork.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from cb_api.security import group_admin
from cb_core import analytics

router = APIRouter(prefix="/groups/{group_id}/analytics", tags=["analytics"])

#: Default window when the caller names neither end. Thirty days is what a
#: dashboard opens on and what the rollup retention comfortably covers.
_DEFAULT_DAYS = 30

#: The widest window one request may ask for. A year of daily rows is 365 rows
#: — small — but the cap is what keeps the endpoint's cost knowable rather
#: than a function of how long the deployment has existed.
_MAX_DAYS = 366


def resolve_window(start: date | None, end: date | None) -> tuple[date, date]:
    """`(start, end)` with the defaults and the cap applied.

    Neither given: the last `_DEFAULT_DAYS` ending today (UTC — the rollups'
    own day boundary). One given: the other is derived from it, so
    `?start=2026-01-01` means "thirty days from then", not "everything since".
    Reversed or too-wide ranges are a 400 rather than a silently clamped
    answer: a caller that asked for the wrong window should learn that, not get
    plausible numbers for a window it did not request.
    """
    today = datetime.now(UTC).date()
    if start is None and end is None:
        end = today
        start = end - timedelta(days=_DEFAULT_DAYS - 1)
    elif start is None:
        assert end is not None
        start = end - timedelta(days=_DEFAULT_DAYS - 1)
    elif end is None:
        end = start + timedelta(days=_DEFAULT_DAYS - 1)

    assert start is not None and end is not None
    if end < start:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="end is before start")
    if (end - start).days + 1 > _MAX_DAYS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"window is longer than {_MAX_DAYS} days",
        )
    return start, end


Window = Annotated[date | None, Query(description="inclusive, UTC")]


class WindowedResponse(BaseModel):
    """The window every answer here echoes back, resolved — the caller may have
    given one end, or neither, and a chart needs to label the axis it actually
    got rather than the one it asked for."""

    group_id: int
    start: date
    end: date


class DailyRow(BaseModel):
    """One day. Days with no activity have no row at all."""

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


class DailyResponse(WindowedResponse):
    days: list[DailyRow]


class CommandRow(BaseModel):
    command: str
    invocations: int
    errors: int
    p95_latency_ms: int | None


class CommandsResponse(WindowedResponse):
    commands: list[CommandRow]


class ModelCostRow(BaseModel):
    provider: str
    model: str
    calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    refusals: int
    errors: int


class LlmResponse(WindowedResponse):
    total_cost_usd: float
    models: list[ModelCostRow]


class SummaryResponse(WindowedResponse):
    """`cb_core.analytics.summarise`, plus the window."""

    days: int = Field(description="how many days had a row, not the window's length")
    messages: int
    commands: int
    joins: int
    leaves: int
    errors: int
    captcha_issued: int
    captcha_solved: int
    captcha_solve_rate: float | None = Field(
        default=None, description="null when nobody was challenged — not the same fact as 0.0"
    )
    peak_active_users: int
    worst_p95_latency_ms: int | None = Field(
        default=None, description="the worst day's p95, never an average of percentiles"
    )
    llm_tokens: int
    llm_cost_usd: float


#: The window is checked before the group is read, so a bad range answers 400
#: even for a group the caller may see; everything else is `security`'s.
_ERRORS: dict[int | str, dict[str, Any]] = {
    400: {"description": "the window is reversed or longer than a year"},
    401: {"description": "no bearer token, or one that did not verify"},
    404: {"description": "no such group — or the caller does not administer it"},
}


@router.get(
    "/daily",
    summary="One row per day the group was active",
    response_model=DailyResponse,
    responses=_ERRORS,
)
async def daily(
    group_id: Annotated[int, Depends(group_admin)],
    start: Window = None,
    end: Window = None,
) -> dict[str, Any]:
    """One row per day the group was active. Days with no activity have no row
    — the rollup writes only what it saw, and inventing zeros here would be
    indistinguishable from a real quiet day."""
    window_start, window_end = resolve_window(start, end)
    rows = await analytics.daily(group_id, window_start, window_end)
    return {
        "group_id": group_id,
        "start": window_start,
        "end": window_end,
        "days": [_daily_row(row) for row in rows],
    }


@router.get(
    "/commands",
    summary="Which commands the group actually uses, busiest first",
    response_model=CommandsResponse,
    responses=_ERRORS,
)
async def commands(
    group_id: Annotated[int, Depends(group_admin)],
    start: Window = None,
    end: Window = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    """Which commands this group actually uses, busiest first, totalled across
    the window rather than broken down per day."""
    window_start, window_end = resolve_window(start, end)
    rows = await analytics.commands(group_id, window_start, window_end, limit=limit)
    return {
        "group_id": group_id,
        "start": window_start,
        "end": window_end,
        "commands": [
            {
                "command": row.command,
                "invocations": row.invocations,
                "errors": row.errors,
                "p95_latency_ms": row.p95_latency_ms,
            }
            for row in rows
        ],
    }


@router.get(
    "/llm",
    summary="What the group's AI features cost, per provider and model",
    response_model=LlmResponse,
    responses=_ERRORS,
)
async def llm(
    group_id: Annotated[int, Depends(group_admin)],
    start: Window = None,
    end: Window = None,
) -> dict[str, Any]:
    """What this group's AI features cost, per provider and model.

    The same numbers `Tenant.monthly_llm_budget_usd` is spent against, so an
    admin can see why the conversational AI started refusing before asking
    anyone.
    """
    window_start, window_end = resolve_window(start, end)
    rows = await analytics.llm_costs(group_id, window_start, window_end)
    return {
        "group_id": group_id,
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
    "/summary",
    summary="The whole window in one object",
    response_model=SummaryResponse,
    responses=_ERRORS,
)
async def summary(
    group_id: Annotated[int, Depends(group_admin)],
    start: Window = None,
    end: Window = None,
) -> dict[str, Any]:
    """The window in one object — totals, the peak day's active users, the
    worst day's p95, and the captcha solve rate (`null` when nobody was
    challenged, which is not the same fact as nobody solving it)."""
    window_start, window_end = resolve_window(start, end)
    rows = await analytics.daily(group_id, window_start, window_end)
    return {
        "group_id": group_id,
        "start": window_start,
        "end": window_end,
        **analytics.summarise(rows),
    }


def _daily_row(row: analytics.DailyStats) -> dict[str, Any]:
    return {
        "day": row.day,
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


__all__ = [
    "CommandsResponse",
    "DailyResponse",
    "LlmResponse",
    "SummaryResponse",
    "resolve_window",
    "router",
]
