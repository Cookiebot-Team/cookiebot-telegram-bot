"""Handler packs — the reader `tenants.handler_pack` never had.

`Tenant.handler_pack` has been on every tenant row since migration `0003` and
was, until this module, written and never read: a tenant naming a pack got the
same handlers as everyone else, silently. `docs/site/content/docs/multi-tenant.mdx`
§"Custom implementations: handler packs" is the design; this is what it looks
like once there is exactly one pack-scoped command family to hang it on
(`x_custom_commands`, v1's `Custom/` prefix).

## One dispatcher, pack-scoped filters — and why that is not the doc's shape

The multi-tenant page describes "one dispatcher per pack", built at startup.
This implements the same observable rule with a filter instead:
`PackProvides(family)` resolves the update's tenant and answers False when that
tenant's pack does not provide the family, so the router falls through exactly
as if the handler had never been registered.

The two are equivalent under the page's own rules — a pack *composes* the core
router and never replaces a core handler (rule 1), so no pack can shadow
another's route, and the only question a pack ever answers is "does this
tenant get this extra command". What the filter buys is that `cb-gateway`
keeps one `Dispatcher`, one webhook route and one `resolve_used_update_types()`
result across every skin; per-pack dispatchers would have to be built at
startup from tenant rows the registry loads lazily, and rebuilt whenever a
tenant's pack changed. The page has been updated to describe what is here.

A pack that eventually needs to *replace* a core handler is the point at which
per-pack dispatchers become the right shape — and rule 1 says that pack should
not exist.

## Families, not routers

A **family** is a name for one group of commands a pack may provide
(`"legacy_custom"` today). Packs map to a frozenset of families; the handler
that implements a family filters on it. Adding a family means adding its
name here and one filter to its router — not a second registration mechanism.
"""

from __future__ import annotations

from typing import Any

from aiogram.filters import BaseFilter
from aiogram.types import Message

from cb_core import tenancy
from cb_core.logging import get_logger

log = get_logger("cb.gateway.packs")

#: v1's GCS `Custom/` prefix: `/<name>` answers with a photo from the pool
#: named after the command (`Miscellaneous.py:145-158`).
LEGACY_CUSTOM = "legacy_custom"

#: pack name -> the families it provides on top of core.
#:
#: `"core"` provides `legacy_custom` because that is v1 parity: the Cookiebot
#: brand has always answered those commands, and its tenant row carries the
#: default pack. `"minimal"` exists so a brand can opt out of the whole family
#: in one field rather than listing 53 command names in `disabled_commands`.
PACKS: dict[str, frozenset[str]] = {
    "core": frozenset({LEGACY_CUSTOM}),
    "minimal": frozenset(),
}

#: What an unknown pack name resolves to. A typo in a tenant row must not
#: silently delete commands, so it degrades to `"core"` and says so once per
#: lookup — the same fail-open rule `TenantCommandGateMiddleware` follows for a
#: registry outage.
_FALLBACK_PACK = "core"


def families_for(pack: str) -> frozenset[str]:
    """Which families `pack` provides. Unknown pack names fall back to core."""
    families = PACKS.get(pack)
    if families is None:
        log.warning("packs.unknown", pack=pack, fallback=_FALLBACK_PACK)
        return PACKS[_FALLBACK_PACK]
    return families


async def tenant_provides(skin: str, family: str) -> bool:
    """Does the tenant behind `skin` get `family`?

    Fails open on a registry outage the same way the dispatch gate does:
    `tenancy.registry.by_skin` never raises (it returns `tenancy.FALLBACK`),
    and `FALLBACK.handler_pack` is `"core"`, so an unreachable database means
    every command still runs.
    """
    tenant = await tenancy.registry.by_skin(skin or tenancy.DEFAULT_TENANT)
    return family in families_for(tenant.handler_pack)


class PackProvides(BaseFilter):
    """Router-level "is this command part of this tenant's pack?".

    Reads `skin` out of the update context — the same key
    `TenantCommandGateMiddleware` and `cb_gateway.main`'s webhook route already
    pass — so it costs one cached tenant lookup per matching command and
    nothing at all for every other update.
    """

    def __init__(self, family: str) -> None:
        self.family = family

    async def __call__(self, message: Message, skin: str = "", **_: Any) -> bool:
        return await tenant_provides(skin, self.family)


__all__ = ["LEGACY_CUSTOM", "PACKS", "PackProvides", "families_for", "tenant_provides"]
