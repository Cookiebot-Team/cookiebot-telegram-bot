"""Tenants: many bots, one core.

v1 already had multi-tenancy, unnamed and hard-coded. Five personas selected by a
CLI argument (`universal_funcs.py:39-52`), event-specific skins per convention
(`core_botskins.feature`), per-group feature flags in the backend `Config`
document, per-group custom commands fetched from a GCS `Custom/` prefix
(`Miscellaneous.py:145-158`), locale packs under `Bot/Static/locales/`, and a
single `ownerID` for all of it. What was missing was a *name* for the concept and
a place to configure it, so every new brand meant another process and another
divergent copy of the caches.

A **tenant** here is a bot brand: its tokens, its owners, its enabled command set,
its branding and locale, its storage prefix and its LLM budget. The shard key is
still `group_id` — tenancy is a logical boundary layered on the physical one, not
a second distribution column. See docs/site/content/docs/multi-tenant.mdx for the rollout.
"""

from __future__ import annotations

import msgspec

from cb_core import cache, db
from cb_core.logging import get_logger

log = get_logger("cb.tenancy")

DEFAULT_TENANT = "cookiebot"
_CACHE_TTL = 300
_CACHE_PREFIX = "cb:tenant:"


class Tenant(msgspec.Struct, frozen=True):
    tenant_id: str
    display_name: str
    # Which handler pack builds this tenant's router. "core" is the shared one;
    # a tenant with bespoke commands ships its own pack and names it here.
    handler_pack: str = "core"
    owner_ids: tuple[int, ...] = ()
    default_locale: str = "en"
    # Commands this tenant turns off even though the pack provides them.
    disabled_commands: frozenset[str] = frozenset()
    # Feature-flag defaults applied to a group when it has no explicit config.
    feature_defaults: dict[str, bool] = {}
    # Per-tenant task -> model overrides, merged over the global CB_LLM_TASKS.
    llm_overrides: dict[str, dict] = {}
    # Blob key prefix. Separate prefixes make per-tenant lifecycle rules and
    # per-tenant buckets possible without touching the key derivation code.
    storage_prefix: str = ""
    # Soft budget; the router refuses `chat` for this tenant once exceeded.
    monthly_llm_budget_usd: float | None = None
    active: bool = True

    def owns(self, user_id: int) -> bool:
        return user_id in self.owner_ids

    def command_enabled(self, command: str) -> bool:
        return command not in self.disabled_commands


#: Used when the database has no tenant rows yet — keeps single-brand deployments
#: working with no configuration at all.
FALLBACK = Tenant(tenant_id=DEFAULT_TENANT, display_name="Cookiebot")

_SELECT = """
SELECT tenant_id, display_name, handler_pack, owner_ids, default_locale,
       disabled_commands, feature_defaults, llm_overrides, storage_prefix,
       monthly_llm_budget_usd, active
FROM tenants WHERE tenant_id = $1
"""

_SELECT_BY_BOT = """
SELECT t.tenant_id, t.display_name, t.handler_pack, t.owner_ids, t.default_locale,
       t.disabled_commands, t.feature_defaults, t.llm_overrides, t.storage_prefix,
       t.monthly_llm_budget_usd, t.active
FROM tenants t JOIN bots b ON b.tenant_id = t.tenant_id
WHERE b.skin = $1
"""


class TenantRegistry:
    """Loads tenants, with an L1 process cache and an L2 shared cache.

    `tenants` is a reference table, so the lookup is node-local from any shard —
    but it is on the per-update path, so it is cached anyway. Invalidation rides
    the existing pub/sub channel rather than v1's manual `/reload`.
    """

    def __init__(self) -> None:
        self._local: dict[str, Tenant] = {}

    async def by_id(self, tenant_id: str) -> Tenant:
        if tenant_id in self._local:
            return self._local[tenant_id]
        tenant = await self._load(_SELECT, tenant_id)
        self._local[tenant_id] = tenant
        return tenant

    async def by_skin(self, skin: str) -> Tenant:
        key = f"skin:{skin}"
        if key in self._local:
            return self._local[key]
        tenant = await self._load(_SELECT_BY_BOT, skin)
        self._local[key] = tenant
        return tenant

    async def _load(self, query: str, arg: str) -> Tenant:
        try:
            row = await db.fetchrow(query, arg, name="tenant_lookup")
        except Exception as exc:  # noqa: BLE001 - a missing tenants table must not stop the bot
            log.warning("tenant.lookup_failed", arg=arg, error=str(exc))
            return FALLBACK
        if row is None:
            return FALLBACK
        return Tenant(
            tenant_id=row["tenant_id"],
            display_name=row["display_name"],
            handler_pack=row["handler_pack"],
            owner_ids=tuple(row["owner_ids"] or ()),
            default_locale=row["default_locale"],
            disabled_commands=frozenset(row["disabled_commands"] or ()),
            feature_defaults=dict(row["feature_defaults"] or {}),
            llm_overrides=dict(row["llm_overrides"] or {}),
            storage_prefix=row["storage_prefix"] or "",
            monthly_llm_budget_usd=(
                float(row["monthly_llm_budget_usd"])
                if row["monthly_llm_budget_usd"] is not None
                else None
            ),
            active=row["active"],
        )

    def cached(self, skin: str) -> Tenant | None:
        """The already-loaded tenant for `skin`, without awaiting anything.

        `cb_core.skins.display_name` needs a brand name in synchronous code
        that has no business opening a database connection for a label. `None`
        means "not loaded yet", never "does not exist" — a caller that needs
        the authoritative answer awaits `by_skin`.
        """
        return self._local.get(f"skin:{skin}") or self._local.get(skin)

    def forget(self, key: str) -> None:
        self._local.pop(key, None)
        self._local.pop(f"skin:{key}", None)

    async def invalidate(self, tenant_id: str) -> None:
        """Drop it here and tell every other replica to do the same."""
        self.forget(tenant_id)
        try:
            await cache.publish_invalidation(f"{_CACHE_PREFIX}{tenant_id}")
        except Exception as exc:  # noqa: BLE001 - cache down must not fail an admin action
            log.warning("tenant.invalidate_failed", tenant_id=tenant_id, error=str(exc))


registry = TenantRegistry()
