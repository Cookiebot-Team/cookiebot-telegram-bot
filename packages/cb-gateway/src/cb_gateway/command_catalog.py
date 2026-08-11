"""The `command_catalog` seam and the pure per-tenant availability predicate.

Both were born in `listcommand.py` (`core_listcommand`, the `/commands` help
listing) because that was the first and, for a while, only place anything
needed to know whether a command exists globally (`command_catalog.enabled`)
or has been switched off for one brand (`tenants.disabled_commands`). Moved
here so `TenantCommandGateMiddleware` (`cb_gateway/middlewares.py`) — the
dispatch-level enforcement `/commands` and everything else was missing —
can reuse the exact same SQL and the exact same rule rather than growing a
second, driftable copy of either. `listcommand.py` still imports both names
from here; nothing about its own behaviour changes.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from cb_core import db
from cb_core.tenancy import Tenant

# command_catalog is a reference table (packages/cb-api/migrations/versions/
# 0001_initial_schema.py) — replicated to every node, so a primary-key lookup is
# node-local wherever it runs; no group_id, no shard fan-out.
_SELECT_CATALOG_ROW = "SELECT command, enabled FROM command_catalog WHERE command = $1"


async def fetch_catalog_row(command: str) -> Mapping[str, Any] | None:
    """The DB seam — a test may monkeypatch this the same way `group_config._fetch_row`
    documents it should be (`cb_core/group_config.py:110-117`); real callers never
    reach past it to asyncpg directly."""
    return await db.fetchrow(_SELECT_CATALOG_ROW, command, name="command_catalog_lookup")


def command_available_for_tenant(row: Mapping[str, Any] | None, tenant: Tenant) -> bool:
    """v2's per-tenant filtering: v1 has no concept of a command catalog or of a
    command being switched off for a whole brand — every command that appears in
    `Cookiebot_functions.txt` is dispatched unconditionally, forever, for every
    v1 process (there was only ever one bot binary per persona, so "which
    commands exist" was a compile-time fact, not configuration).

    `command_catalog` (reference table) plus `tenants.disabled_commands`
    (`cb_core/tenancy.py`) give a brand built on the shared "core" handler pack a
    way to turn a command off without a code change — the thing `is_alternate_bot`
    used to require a whole separate process for (FEATURE-MAP `core_botskins`).

    This is pure and DB-free on purpose: it is the one piece of "should this
    command run" logic worth unit-testing in isolation from any I/O, and it is
    now shared by two callers (`/commands`' own listing and the dispatch gate)
    that must never disagree about what "disabled" means.

    A command absent from the catalog, or explicitly disabled there, does not
    exist for anyone — `enabled` is the global kill switch. Per-tenant opt-out is
    layered on top of that, never instead of it.
    """
    if row is None or not row["enabled"]:
        return False
    return tenant.command_enabled(row["command"])


__all__ = ["command_available_for_tenant", "fetch_catalog_row"]
