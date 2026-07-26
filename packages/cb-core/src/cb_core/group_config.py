"""Group configuration: the table every gated command needs and nothing read.

`group_configs` has existed since `0001_initial_schema.py:158-180` with no reader
and no writer. v1's failure mode is the thing this replaces: five bot processes
each held an unlocked `cache_configurations` dict with no TTL
(`Configurations.py:9-12`, `:103-137`), so a config change needed a manual
`/reload` typed into *every* process (`COOKIEBOT.py:197-201`) and they still
drifted (FEATURE-MAP D6). See `docs/contracts/group-config.md` for the full v1/v2
field mapping, the v1 defaults with file:line, and the mismatches this port found
and deliberately did not paper over with a migration.

Read path: L1 (per-process dict, short TTL) -> L2 (valkey, longer TTL) -> Postgres,
single-shard on `group_id`. Merge order low to high: `DEFAULTS` < tenant
`feature_defaults` < the group's own row. A write goes through `set_config`, which
upserts then invalidates L2 and publishes on the shared invalidation channel so
every replica drops its L1 copy — the fix for v1's "type /reload five times".
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from cb_core import cache, db, metrics, tenancy
from cb_core.logging import get_logger
from cb_core.settings import get_settings

log = get_logger("cb.group_config")

_CACHE = "config"
_CACHE_PREFIX = "cb:groupconfig:"

# v1 -> v2 feature-area names used by handlers (`funfunctions`/`utilityfunctions`
# gating at COOKIEBOT.py:218 and :252).
_FEATURE_AREAS = {"fun": "functions_fun", "utility": "functions_utility"}


@dataclasses.dataclass(frozen=True, slots=True)
class GroupConfig:
    """One row of `group_configs`, plus the shard key.

    Field-for-field mapping to v1 and the SQL columns is in
    `docs/contracts/group-config.md`.
    """

    group_id: int
    allow_furbots: bool = True
    sticker_spam_limit: int = 5
    sticker_spam_window_s: int = 60
    media_restrict_seconds: int = 600
    captcha_timeout_seconds: int = 300
    functions_fun: bool = True
    functions_utility: bool = True
    sfw: bool = True
    language: str = "en"
    publisher_post: bool = False
    publisher_ask: bool = True
    publisher_members_only: bool = False
    # v1 writes the string "9999" to mean "no topic pinned"; v2 says NULL. One
    # sentinel, not two, or every reader has to know both — the M4 Mongo ETL
    # converts "9999" to NULL on the way in (docs/contracts/group-config.md).
    thread_posts: str | None = None
    max_posts: int = 9999
    doomlist_enabled: bool = True

    def feature_enabled(self, area: str) -> bool:
        """`'fun'` -> `functions_fun`, `'utility'` -> `functions_utility`.

        Mirrors the two gates in v1's dispatcher (`COOKIEBOT.py:218`, `:252`).
        """
        try:
            column = _FEATURE_AREAS[area]
        except KeyError:
            raise ValueError(f"unknown feature area: {area!r}") from None
        return bool(getattr(self, column))


# Columns writable through set_config(); never group_id (the shard key, immutable
# after insert). Also the shape of a row read back from group_configs.
_COLUMNS: tuple[str, ...] = tuple(
    f.name for f in dataclasses.fields(GroupConfig) if f.name != "group_id"
)
_WRITABLE_COLUMNS = frozenset(_COLUMNS)

#: v1's real defaults, sourced from `Configurations.py:111` (the Java `Config`
#: entity carries no defaults at all — see the contract doc). `language` is the
#: one deliberate exception: it comes from `settings.default_language` for a
#: brand-new, tenant-less deployment rather than v1's hardcoded `"pt"`.
DEFAULTS = GroupConfig(group_id=0, language=get_settings().default_language)

_SELECT = """
SELECT g.tenant_id,
       gc.group_id AS config_group_id,
       gc.allow_furbots, gc.sticker_spam_limit, gc.sticker_spam_window_s,
       gc.media_restrict_seconds, gc.captcha_timeout_seconds, gc.functions_fun,
       gc.functions_utility, gc.sfw, gc.language, gc.publisher_post,
       gc.publisher_ask, gc.publisher_members_only, gc.thread_posts,
       gc.max_posts, gc.doomlist_enabled
FROM groups g
LEFT JOIN group_configs gc ON gc.group_id = g.group_id
WHERE g.group_id = $1
"""


async def _fetch_row(group_id: int) -> Mapping[str, Any] | None:
    """The DB seam. Unit tests monkeypatch this function, never asyncpg internals.

    A single query, filtered on `group_id` — one shard, no fan-out (AGENTS.md §4).
    `groups` LEFT JOIN `group_configs` recovers the tenant in the same round trip
    instead of a second lookup.
    """
    return await db.fetchrow(_SELECT, group_id, name="group_config_lookup")


# --------------------------------------------------------------------------- L1

_l1: dict[int, tuple[GroupConfig, float]] = {}


def cached_size() -> int:
    """Number of live L1 entries — for tests, not a metric (no group_id labels)."""
    return len(_l1)


def _l1_get(group_id: int) -> GroupConfig | None:
    entry = _l1.get(group_id)
    if entry is None:
        return None
    config, expires_at = entry
    if time.monotonic() >= expires_at:
        _l1.pop(group_id, None)
        return None
    return config


def _l1_set(group_id: int, config: GroupConfig) -> None:
    ttl = get_settings().config_cache_l1_seconds
    _l1[group_id] = (config, time.monotonic() + ttl)


def _l1_drop(group_id: int) -> None:
    _l1.pop(group_id, None)


# --------------------------------------------------------------------------- L2


def _l2_key(group_id: int) -> str:
    return f"{_CACHE_PREFIX}{group_id}"


async def _l2_get(group_id: int) -> GroupConfig | None:
    try:
        data = await cache.get_json(_l2_key(group_id))
    except Exception as exc:  # noqa: BLE001 - a down cache must degrade, not raise
        log.warning("group_config.l2_read_failed", group_id=group_id, error=str(exc))
        return None
    if data is None:
        return None
    return GroupConfig(**data)


async def _l2_set(group_id: int, config: GroupConfig) -> None:
    try:
        await cache.set_json(
            _l2_key(group_id),
            dataclasses.asdict(config),
            get_settings().config_cache_l2_seconds,
        )
    except Exception as exc:  # noqa: BLE001 - caching is best-effort, never fatal
        log.warning("group_config.l2_write_failed", group_id=group_id, error=str(exc))


# ------------------------------------------------------------------------ merge


def _apply_tenant_defaults(base: GroupConfig, tenant: tenancy.Tenant) -> GroupConfig:
    """Layer a tenant's boolean feature flags over `base`. Unknown keys are ignored."""
    # dict[str, Any], not dict[str, bool]: `_WRITABLE_COLUMNS` spans every column,
    # not just the boolean ones, so `dataclasses.replace`'s per-field check needs
    # the value type to cover every writable field, not just the bool-typed ones.
    overrides: dict[str, Any] = {
        k: v for k, v in tenant.feature_defaults.items() if k in _WRITABLE_COLUMNS
    }
    return dataclasses.replace(base, **overrides) if overrides else base


async def _build_config(group_id: int, row: Mapping[str, Any] | None) -> GroupConfig:
    tenant_id = row["tenant_id"] if row is not None else None
    tenant = await tenancy.registry.by_id(tenant_id or tenancy.DEFAULT_TENANT)
    base = _apply_tenant_defaults(dataclasses.replace(DEFAULTS, group_id=group_id), tenant)

    # v2's schema makes every group_configs column NOT NULL, so a present row is
    # always fully populated — same as v1, where a config document either existed
    # complete or not at all (Configurations.py:103-137). The LEFT JOIN reports
    # "no row" as every gc.* column, including config_group_id, being NULL.
    if row is None or row["config_group_id"] is None:
        return base

    return GroupConfig(group_id=group_id, **{column: row[column] for column in _COLUMNS})


# ------------------------------------------------------------------------- read


async def get_config(group_id: int) -> GroupConfig:
    cached = _l1_get(group_id)
    if cached is not None:
        metrics.cache_lookups_total.labels(cache=_CACHE, layer="l1", outcome="hit").inc()
        return cached
    metrics.cache_lookups_total.labels(cache=_CACHE, layer="l1", outcome="miss").inc()

    l2_hit = await _l2_get(group_id)
    if l2_hit is not None:
        metrics.cache_lookups_total.labels(cache=_CACHE, layer="l2", outcome="hit").inc()
        _l1_set(group_id, l2_hit)
        return l2_hit
    metrics.cache_lookups_total.labels(cache=_CACHE, layer="l2", outcome="miss").inc()

    try:
        row = await _fetch_row(group_id)
        config = await _build_config(group_id, row)
    except Exception as exc:  # noqa: BLE001 - a config read must never break a reply
        log.warning("group_config.db_failed", group_id=group_id, error=str(exc))
        metrics.cache_lookups_total.labels(cache=_CACHE, layer="db", outcome="error").inc()
        metrics.config_fallback_total.labels(reason="db_error").inc()
        return dataclasses.replace(DEFAULTS, group_id=group_id)

    metrics.cache_lookups_total.labels(cache=_CACHE, layer="db", outcome="hit").inc()
    _l1_set(group_id, config)
    await _l2_set(group_id, config)
    return config


# ------------------------------------------------------------------------ write


async def set_config(group_id: int, **fields: object) -> GroupConfig:
    """Upsert the given columns, then invalidate every layer everywhere.

    Column names come from the whitelist below, never from string-building on
    caller input; values are bound parameters. Replaces v1's "send /reload in the
    chat if the old config persists" (`Configurations.py:209`) with real
    invalidation.
    """
    unknown = set(fields) - _WRITABLE_COLUMNS
    if unknown:
        raise ValueError(f"unknown group_configs column(s): {sorted(unknown)}")
    if not fields:
        return await get_config(group_id)

    columns = list(fields)
    values = [fields[c] for c in columns]
    insert_columns = ", ".join(columns)
    # updated_at is bound once here in Python, not `now()` in the DO UPDATE SET:
    # Citus rejects non-IMMUTABLE functions there on a distributed table, because
    # each shard would evaluate its own (same fix as cb_rollup_day, 0001_initial_schema.py:436-440).
    now = datetime.now(UTC)
    insert_placeholders = ", ".join(f"${i + 2}" for i in range(len(columns)))
    update_assignments = ", ".join(f"{c} = EXCLUDED.{c}" for c in columns)
    stmt = (
        f"INSERT INTO group_configs (group_id, {insert_columns}, updated_at) "
        f"VALUES ($1, {insert_placeholders}, ${len(columns) + 2}) "
        f"ON CONFLICT (group_id) DO UPDATE SET {update_assignments}, updated_at = EXCLUDED.updated_at"
    )
    await db.execute(stmt, group_id, *values, now, name="group_config_upsert")
    await invalidate(group_id)
    return await get_config(group_id)


async def invalidate(group_id: int) -> None:
    """Drop the L1 entry here, clear L2, and tell every other replica to drop L1."""
    _l1_drop(group_id)
    try:
        await cache.delete(_l2_key(group_id))
    except Exception as exc:  # noqa: BLE001 - a down cache must not fail a write
        log.warning("group_config.l2_invalidate_failed", group_id=group_id, error=str(exc))
    try:
        await cache.publish_invalidation(_l2_key(group_id))
        metrics.cache_invalidations_total.labels(cache=_CACHE, direction="published").inc()
    except Exception as exc:  # noqa: BLE001 - a down cache must not fail a write
        log.warning("group_config.publish_failed", group_id=group_id, error=str(exc))


# ------------------------------------------------------------------- listener

_listener_task: asyncio.Task[None] | None = None
_listener_pubsub: Any | None = None


def _on_invalidate_key(key: str) -> None:
    """Callback for `cb_core.cache.subscribe_invalidations` — drops the local L1 copy."""
    if not key.startswith(_CACHE_PREFIX):
        return
    try:
        group_id = int(key[len(_CACHE_PREFIX) :])
    except ValueError:
        return
    _l1_drop(group_id)
    metrics.cache_invalidations_total.labels(cache=_CACHE, direction="received").inc()


async def start_invalidation_listener() -> None:
    global _listener_task, _listener_pubsub
    if _listener_task is not None:
        return
    _listener_task, _listener_pubsub = await cache.subscribe_invalidations(_on_invalidate_key)


async def stop_invalidation_listener() -> None:
    global _listener_task, _listener_pubsub
    if _listener_task is not None:
        _listener_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _listener_task
        _listener_task = None
    if _listener_pubsub is not None:
        await _listener_pubsub.unsubscribe(cache.INVALIDATION_CHANNEL)
        await _listener_pubsub.aclose()
        _listener_pubsub = None
