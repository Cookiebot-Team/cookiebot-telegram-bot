"""core_listcommand — `/commands` / `/comandos`, the static help text.

v1: `list_commands`, `../COOKIEBOT-Telegram-Group-Bot/Bot/Miscellaneous.py:124-127`::

    def list_commands(cookiebot, msg, chat_id, language):
        send_chat_action(cookiebot, chat_id, 'typing')
        string = i18n.get_file("Cookiebot_functions.txt", lang=language)
        send_message(cookiebot, chat_id, string, msg_to_reply=msg)

Dispatched unconditionally from two places in
`../COOKIEBOT-Telegram-Group-Bot/Bot/COOKIEBOT.py` — no admin check, and,
critically, **no `functionsFun`/`functionsUtility` gate**: the group-chat arm
(`COOKIEBOT.py:276-277`) sits *after* the fun/utility elif blocks in the chain,
as its own unconditional `elif`, so it fires whether or not either feature area
is switched off. The private-chat arm (`COOKIEBOT.py:85-86`) hardcodes
`language='eng'` and never looks at a group at all — there is no group to look
one up for.

The text itself is `cb_core.locales.text("Cookiebot_functions", lang)`, already
ported byte-for-byte from `Bot/Static/locales/{eng,pt,es}/Cookiebot_functions.txt`
— never retyped or reformatted here.

See `docs/contracts/core_listcommand.md` for the full Phase 2/6 contract,
including what "per-tenant filtering" means on top of v1's unconditional
dispatch.

The private-chat branch is its own `F.chat.type == ChatType.PRIVATE`-filtered
handler rather than a branch inside the group one — relocated, not
rebehaviored, to the shared pattern `.specs/features/private_dispatch/`
establishes (`cb_gateway/private_context.py`'s module docstring has the full
reasoning). This file was already the one place in the codebase that got the
private-chat case right on its own — avoiding `context_for` for a DM instead
of trying to make it safe for one — so there is nothing to fix here, only to
line up with the now-shared shape.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.types import Message

from cb_core import db, locales, tenancy
from cb_core.logging import get_logger
from cb_core.tenancy import Tenant
from cb_gateway.context import context_for
from cb_gateway.filters import CommandName
from cb_gateway.telemetry import mark_outcome

log = get_logger("cb.gateway.listcommand")

router = Router(name="listcommand")

_CATALOG_COMMAND = "commands"

# command_catalog is a reference table (packages/cb-api/migrations/versions/
# 0001_initial_schema.py) — replicated to every node, so a primary-key lookup is
# node-local wherever it runs; no group_id, no shard fan-out.
_SELECT_CATALOG_ROW = "SELECT command, enabled FROM command_catalog WHERE command = $1"


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

    This is pure and DB-free on purpose: it is the one piece of "should /commands
    answer" logic worth unit-testing in isolation from any I/O.

    A command absent from the catalog, or explicitly disabled there, does not
    exist for anyone — `enabled` is the global kill switch. Per-tenant opt-out is
    layered on top of that, never instead of it.
    """
    if row is None or not row["enabled"]:
        return False
    return tenant.command_enabled(row["command"])


async def _fetch_catalog_row(command: str) -> Mapping[str, Any] | None:
    """The DB seam — a test may monkeypatch this the same way `group_config._fetch_row`
    documents it should be (`cb_core/group_config.py:110-117`); real callers never
    reach past it to asyncpg directly."""
    return await db.fetchrow(_SELECT_CATALOG_ROW, command, name="listcommand_catalog_lookup")


async def _commands_available(skin: str) -> bool:
    """Resolve the tenant for the bot this update arrived through, then check the
    catalog. Fails open: a catalog or tenant-registry outage must not hide the one
    command that explains what the bot can do (AGENTS.md §2.6's "never go silent
    on an infra hiccup", extended from analytics to this UX nicety) — and for a
    single-tenant deployment with the seeded defaults (`'cookiebot'` tenant, empty
    `disabled_commands`; `command_catalog.commands.enabled = true`,
    `0001_initial_schema.py:487`) this always resolves `True`, so `/commands`
    behaves exactly as v1's unconditional dispatch.

    `tenancy.registry.by_skin` already never raises (falls back to
    `tenancy.FALLBACK` on any lookup failure); only the catalog read can raise
    here.
    """
    try:
        tenant = await tenancy.registry.by_skin(skin)
        row = await _fetch_catalog_row(_CATALOG_COMMAND)
    except Exception as exc:  # noqa: BLE001 - a catalog outage must not hide the help text
        log.warning("listcommand.catalog_lookup_failed", skin=skin, error=str(exc))
        return True
    return command_available_for_tenant(row, tenant)


@router.message(F.chat.type == ChatType.PRIVATE, CommandName("commands"))
async def list_commands_private(message: Message, skin: str = tenancy.DEFAULT_TENANT) -> None:
    """v1's private-chat branch (`COOKIEBOT.py:85-86`) — hardcoded `'eng'`,
    never consults a group config, because there is no group to look one up
    for. No `context_for` call, on purpose (module docstring)."""
    if not await _commands_available(skin):
        mark_outcome("silent")
        return
    await message.reply(locales.text("Cookiebot_functions", "en"))


@router.message(F.chat.type != ChatType.PRIVATE, CommandName("commands"))
async def list_commands(
    message: Message, bot: Bot | None = None, skin: str = tenancy.DEFAULT_TENANT
) -> None:
    """`/commands` / `/comandos` (aliases already in `cb_core/textmatch.py`).

    No `FeatureGate`, no `AdminOnly` — matches v1's unconditional dispatch: the
    fun/utility gates decide whether *those* commands run, never whether they are
    listed (see the module docstring and `docs/contracts/core_listcommand.md`).
    """
    if not await _commands_available(skin):
        # A tenant that switched /commands off, or a catalog row that disabled it
        # globally — deliberate silence, not a lookup failure (that path fails
        # open and answers, see `_commands_available`'s docstring).
        mark_outcome("silent")
        return

    ctx = await context_for(cast(Bot, bot or message.bot), message)
    await message.reply(locales.text("Cookiebot_functions", ctx.lang))
