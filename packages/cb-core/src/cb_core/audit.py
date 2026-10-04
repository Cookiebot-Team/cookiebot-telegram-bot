"""x_audit_log — who changed what in a group, and when.

Net-new. v1 kept no trail at all: a setting changed in `/config` left the old
value nowhere, and "who turned the captcha off" was answerable only by asking
the admins. The Mini App makes that worse rather than better — a second surface
onto the same settings — so every write goes through here, whichever surface
made it.

Two rules give the table its shape (see `0010_miniapp_sessions_and_audit.py`):

* **Rows are evidence, so they quote.** `before`/`after` hold the fields that
  actually changed, with their old and new values, not a rendered sentence. A
  human-readable `summary` is stored beside them for display, never instead of
  them.
* **A failed audit write never rewrites history and never fails the caller.**
  The action it describes has already happened by the time `record` runs; a
  500 at that point would tell the client the change did not take when it did.
  The row is lost loudly instead — `log.error` plus
  `cb_audit_write_failures_total` — which is the honest version of the trade.

Reads are keyset-paginated on the UUIDv7 primary key (D11: no unbounded list),
which is chronological by construction, so "the next page" is `before_id` and
never an `OFFSET`.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from cb_core import db, ids, metrics
from cb_core.logging import get_logger
from cb_core.telemetry import current_trace_id

log = get_logger("cb.audit")

# --------------------------------------------------------------------- actions
# One string per kind of act, `<subject>.<verb>`. They are values in an API
# response and a filter a client passes back, so they are part of the contract:
# add freely, rename never.
CONFIG_UPDATED = "config.updated"
RULES_UPDATED = "rules.updated"
WELCOME_UPDATED = "welcome.updated"
SESSION_STARTED = "session.started"

#: Where the action came from. `telegram` is a command or a menu press in the
#: chat; `miniapp` and `api` are HTTP callers; `system` is the bot itself.
SURFACES = ("telegram", "miniapp", "api", "system")

Surface = Literal["telegram", "miniapp", "api", "system"]


@dataclasses.dataclass(frozen=True, slots=True)
class AuditEvent:
    id: UUID
    group_id: int
    ts: datetime
    action: str
    surface: str
    actor_user_id: int | None = None
    actor_kind: str = "admin"
    summary: str | None = None
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    trace_id: str | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class AuditSlice:
    """One page plus the cursor for the next, or `None` on the last page.

    `next_before` is set only when an older matching row exists (D1): the read
    fetches `limit + 1` and the extra row is the proof, never `len == limit`.
    """

    events: tuple[AuditEvent, ...]
    next_before: UUID | None


@dataclasses.dataclass(frozen=True, slots=True)
class FleetAuditEvent:
    """An event from the fleet read, with the title of the group it belongs to."""

    event: AuditEvent
    group_title: str | None


@dataclasses.dataclass(frozen=True, slots=True)
class FleetAuditSlice:
    events: tuple[FleetAuditEvent, ...]
    next_before: UUID | None


_INSERT = """
INSERT INTO group_audit_events (
    group_id, id, ts, actor_user_id, actor_kind, action, surface,
    summary, before, after, trace_id
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
"""

# `id` is a v7 UUID, so ordering by it is ordering by time — and the keyset
# predicate is the same column, which is why no `ts` index exists.
_PAGE = """
SELECT group_id, id, ts, actor_user_id, actor_kind, action, surface,
       summary, before, after, trace_id
  FROM group_audit_events
 WHERE group_id = $1
   AND ($2::uuid IS NULL OR id < $2::uuid)
   AND ($3::text IS NULL OR action = $3::text)
   AND ($4::bigint IS NULL OR actor_user_id = $4::bigint)
   AND ($5::text IS NULL OR surface = $5::text)
   AND ($6::timestamptz IS NULL OR ts >= $6::timestamptz)
   AND ($7::timestamptz IS NULL OR ts < $7::timestamptz)
 ORDER BY id DESC
 LIMIT $8
"""

# The fleet read. `groups` is colocated with `group_id`, so the join is pushed
# down per shard. `{group_filter}` is spliced as one of two fixed fragments (never
# user input). With a group the predicate is a bare `e.group_id = $2` so the router
# can prune to one shard (proven by the one-shard EXPLAIN test, which uses a
# custom plan); an `($2 IS NULL OR ...)` form is avoided on purpose as it gives
# the planner no direct equality on the shard key.
_FLEET_PAGE = """
SELECT e.group_id, e.id, e.ts, e.actor_user_id, e.actor_kind, e.action, e.surface,
       e.summary, e.before, e.after, e.trace_id, g.title AS group_title
  FROM group_audit_events e
  JOIN groups g ON g.group_id = e.group_id
 WHERE g.tenant_id = $1
   {group_filter}
   AND ($3::uuid IS NULL OR e.id < $3::uuid)
   AND ($4::text IS NULL OR e.action = $4::text)
   AND ($5::bigint IS NULL OR e.actor_user_id = $5::bigint)
   AND ($6::text IS NULL OR e.surface = $6::text)
   AND ($7::timestamptz IS NULL OR e.ts >= $7::timestamptz)
   AND ($8::timestamptz IS NULL OR e.ts < $8::timestamptz)
 ORDER BY e.id DESC
 LIMIT $9
"""
_FLEET_ALL_GROUPS = "AND $2::bigint IS NULL"
_FLEET_ONE_GROUP = "AND e.group_id = $2::bigint"


def fleet_sql(group_id: int | None) -> str:
    """The fleet statement for a given-or-not `group_id` (public for EXPLAIN tests)."""
    fragment = _FLEET_ONE_GROUP if group_id is not None else _FLEET_ALL_GROUPS
    return _FLEET_PAGE.replace("{group_filter}", fragment)


def diff(before: dict[str, Any], after: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """The fields that actually changed, on both sides.

    An admin who saves a form without touching it should not produce a row that
    looks like they rewrote every setting — so a value equal on both sides is
    dropped from the pair, and an empty result means there is nothing to record.
    """
    changed = [key for key in after if before.get(key) != after[key]]
    return ({key: before.get(key) for key in changed}, {key: after[key] for key in changed})


async def record(
    group_id: int,
    action: str,
    *,
    actor_user_id: int | None = None,
    actor_kind: str = "admin",
    surface: str = "api",
    summary: str | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> AuditEvent | None:
    """Write one row. Returns it, or `None` when the write failed.

    Callers do not check the return value for control flow — see the module
    docstring — but tests and the endpoints that echo the row back do.
    """
    event = AuditEvent(
        id=ids.uuid7(),
        group_id=group_id,
        ts=now or _now(),
        action=action,
        surface=surface,
        actor_user_id=actor_user_id,
        actor_kind=actor_kind,
        summary=summary,
        before=before,
        after=after,
        trace_id=current_trace_id(),
    )
    try:
        await db.execute(
            _INSERT,
            event.group_id,
            event.id,
            event.ts,
            event.actor_user_id,
            event.actor_kind,
            event.action,
            event.surface,
            event.summary,
            _json(event.before),
            _json(event.after),
            event.trace_id,
            name="audit_insert",
        )
    except Exception as exc:  # noqa: BLE001 - the audited action already happened
        log.error(
            "audit.write_failed",
            group_id=group_id,
            action=action,
            surface=surface,
            error=str(exc),
        )
        metrics.audit_write_failures_total.labels(action=action).inc()
        return None
    metrics.audit_events_total.labels(action=action, surface=surface).inc()
    return event


async def page(
    group_id: int,
    *,
    limit: int = 50,
    before_id: UUID | None = None,
    action: str | None = None,
    actor_user_id: int | None = None,
    surface: Surface | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> AuditSlice:
    """One page, newest first. `before_id` is the last id of the previous page.

    Single-shard: `group_id` is the first predicate. `since` is inclusive and
    `until` exclusive, on `ts`; naive datetimes are read as UTC.
    """
    if limit < 1:
        raise ValueError("limit must be at least 1")
    rows = await db.fetch(
        _PAGE,
        group_id,
        before_id,
        action,
        actor_user_id,
        surface,
        _utc(since),
        _utc(until),
        limit + 1,
        name="audit_page",
    )
    events = tuple(_from_row(row) for row in rows[:limit])
    return AuditSlice(events, events[-1].id if len(rows) > limit else None)


async def fleet_page(
    *,
    tenant_id: str,
    limit: int = 50,
    before: UUID | None = None,
    group_id: int | None = None,
    action: str | None = None,
    actor_user_id: int | None = None,
    surface: Surface | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> FleetAuditSlice:
    """The tenant's audit trail across every group, newest first.

    Like `cb_core.platform_analytics`, this is a deliberate fan-out: with no
    `group_id` it omits the shard key, which AGENTS.md section 4.1 forbids on the
    reply path. It is acceptable here because it is owner-only (a handful of
    callers), never on a message's reply path, and keyset + `LIMIT` bound it:
    each shard returns at most `limit + 1` rows, served from the
    `group_audit_events (id DESC)` index without a sort. The tenant is scoped
    through `groups.tenant_id`, colocated on `group_id`, exactly as
    `cb_core.llm.budget` does. With `group_id` given the read is single-shard.

    The tenant follows the group's *current* `groups.tenant_id`: re-tenanting a
    group moves its whole audit history to the new tenant's view.
    """
    if limit < 1:
        raise ValueError("limit must be at least 1")
    rows = await db.fetch(
        fleet_sql(group_id),
        tenant_id,
        group_id,
        before,
        action,
        actor_user_id,
        surface,
        _utc(since),
        _utc(until),
        limit + 1,
        name="audit_fleet_page" if group_id is None else "audit_fleet_group_page",
    )
    events = tuple(FleetAuditEvent(_from_row(row), row["group_title"]) for row in rows[:limit])
    return FleetAuditSlice(events, events[-1].event.id if len(rows) > limit else None)


def _utc(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value


def _from_row(row: Any) -> AuditEvent:
    return AuditEvent(
        id=row["id"],
        group_id=row["group_id"],
        ts=row["ts"],
        action=row["action"],
        surface=row["surface"],
        actor_user_id=row["actor_user_id"],
        actor_kind=row["actor_kind"],
        summary=row["summary"],
        before=_loads(row["before"]),
        after=_loads(row["after"]),
        trace_id=row["trace_id"],
    )


def _json(value: dict[str, Any] | None) -> str | None:
    """jsonb wants text over the wire; asyncpg does not encode dicts itself."""
    return None if value is None else json.dumps(value, default=str)


def _loads(value: Any) -> dict[str, Any] | None:
    if value is None or isinstance(value, dict):
        return value
    return dict(json.loads(value))


def _now() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "CONFIG_UPDATED",
    "RULES_UPDATED",
    "SESSION_STARTED",
    "SURFACES",
    "WELCOME_UPDATED",
    "AuditEvent",
    "AuditSlice",
    "FleetAuditEvent",
    "FleetAuditSlice",
    "Surface",
    "diff",
    "fleet_page",
    "fleet_sql",
    "page",
    "record",
]
