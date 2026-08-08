"""core_botskins — what makes a skin more than a token.

v1 selected a persona with a CLI argument and carried it as `is_alternate_bot`
through every call (`../COOKIEBOT-Telegram-Group-Bot/Bot/universal_funcs.py:39-52`,
`COOKIEBOT.py:24-32`). `cb_gateway.bots.BotRegistry` already replaced the
*process* half of that — one process serves every skin — but the behavioural
half was never ported, and `.specs/features/core_botskins/spec.md` recorded
exactly that: "the 'skin' is currently only a token and a display name, not an
experience."

This module is the missing half. It answers the two questions v1 asked of
`is_alternate_bot` that are still meaningful in v2, plus the asset lookup a
branded skin needs.

## The two behavioural differences v1 actually has

Grepping `is_alternate_bot` in `COOKIEBOT.py` for uses that are *not* just
threading it through to `get_bot_token` gives four sites. Two of them are v2
irrelevant — `:333` (only the primary process triggered the daily birthday
sweep) and `:459` (only the primary process ran the scheduler and the API
server) are both `cb-worker`'s and `cb-api`'s jobs now, and neither is a
per-skin behaviour any more. The other two are real, user-visible, and are
what `is_primary` exists for:

* **`:130`** — `if not is_alternate_bot:` around the celebratory animation the
  bot posts when it is added to a group. An alternate skin joins quietly.
* **`:143`** — `if (funfunctions or is_alternate_bot) and randint(1,10) == 1:`
  around the "silence scammer" photo posted after a join-time ban check fires.
  An alternate skin posts it **regardless of the group's fun setting**.

## Assets

`asset(skin, *parts)` resolves a per-skin override first
(`asset_data/skins/<skin>/<parts>`) and falls back to the shared file
(`asset_data/<parts>`). That is the whole "asset pack" mechanism: a new brand
adds a directory, overrides the files it wants, and inherits the rest. Nothing
else in the codebase has to know a skin exists.
"""

from __future__ import annotations

from pathlib import Path

from cb_core import assets, tenancy

#: v1's `is_alternate_bot == 0` — the flagship persona. Everything else is an
#: "alternate" in v1's vocabulary.
PRIMARY_SKIN = tenancy.DEFAULT_TENANT

#: Where a skin's overrides live, under `cb_core.asset_data`.
SKIN_PACK_DIR = "skins"

#: The animation v1 posts when it is added to a group (`COOKIEBOT.py:131`),
#: hardcoded there as a Dribbble CDN URL. Kept verbatim: it is what live
#: groups have seen, and it is a URL rather than a file precisely so no asset
#: has to ship with it.
INTRO_ANIMATION_URL = (
    "https://cdn.dribbble.com/users/4228736/screenshots/10874431/media/"
    "28ef00faa119065224429a0f94be21f3.gif"
)


def is_primary(skin: str) -> bool:
    """v1's `not is_alternate_bot`.

    Anything that is not the flagship skin is an alternate — including an
    unknown skin, which is the safe direction: an unrecognised brand should
    not inherit the flagship's join announcement.
    """
    return skin == PRIMARY_SKIN


def posts_intro_animation(skin: str) -> bool:
    """v1 `COOKIEBOT.py:130` — only the flagship announces itself on joining."""
    return is_primary(skin)


def scammer_photo_allowed(skin: str, *, fun_enabled: bool) -> bool:
    """v1 `COOKIEBOT.py:143` — `funfunctions or is_alternate_bot`.

    An event skin posts the flair even in a group that has turned fun features
    off. That reads backwards until you notice what an event skin is *for*: it
    is invited to a convention's group to be a mascot, and v1 treats "this is
    the bot's whole purpose here" as outranking the group's own switch.
    """
    return fun_enabled or not is_primary(skin)


def asset(skin: str, *parts: str) -> Path:
    """A static asset, with the skin's own version preferred.

    Falls back to the shared file, so a skin only has to ship what it actually
    rebrands. Resolution goes through `cb_core.assets.path`, which is what
    still finds a file once this is an installed wheel rather than a checkout.
    """
    override = assets.path(SKIN_PACK_DIR, skin, *parts)
    if override.is_file():
        return override
    return assets.path(*parts)


def display_name(skin: str) -> str:
    """What this brand calls itself. The tenant registry owns it; this is the
    synchronous, cache-only read a handler can make without awaiting."""
    tenant = tenancy.registry.cached(skin)
    return tenant.display_name if tenant is not None else skin


__all__ = [
    "INTRO_ANIMATION_URL",
    "PRIMARY_SKIN",
    "SKIN_PACK_DIR",
    "asset",
    "display_name",
    "is_primary",
    "posts_intro_animation",
    "scammer_photo_allowed",
]
