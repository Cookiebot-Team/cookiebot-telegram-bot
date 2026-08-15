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
from datetime import datetime
from typing import Any
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
 ORDER BY id DESC
 LIMIT $5
"""


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
) -> tuple[AuditEvent, ...]:
    """One page, newest first. `before_id` is the last id of the previous page."""
    rows = await db.fetch(
        _PAGE,
        group_id,
        before_id,
        action,
        actor_user_id,
        limit,
        name="audit_page",
    )
    return tuple(_from_row(row) for row in rows)


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
    from datetime import UTC

    return datetime.now(UTC)


__all__ = [
    "CONFIG_UPDATED",
    "RULES_UPDATED",
    "SESSION_STARTED",
    "SURFACES",
    "WELCOME_UPDATED",
    "AuditEvent",
    "diff",
    "page",
    "record",
]
