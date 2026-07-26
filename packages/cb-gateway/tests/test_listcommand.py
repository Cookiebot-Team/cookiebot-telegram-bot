"""Unit coverage for core_listcommand — trigger surface and catalog filtering.

Pure logic and monkeypatched seams only (no dispatcher, no Telegram, no real DB):
every v1 alias must resolve through `CommandName`, and `command_available_for_tenant`
— the pure function behind v2's per-tenant filtering — must reduce to v1's
unconditional "always show the list" for the seeded single-tenant shape. See
docs/contracts/core_listcommand.md for the full behaviour contract and
qa/features/core_listcommand.feature / qa/integration/test_command_catalog.py for
the end-to-end and DB-backed versions of the same assertions.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import pytest

from cb_core.tenancy import FALLBACK, Tenant
from cb_gateway.filters import CommandName
from cb_gateway.handlers.listcommand import _commands_available, command_available_for_tenant


@dataclass
class _FakeMessage:
    """CommandName only ever reads `.text` off the message it's given."""

    text: str | None


# --------------------------------------------------------------------- triggers


@pytest.mark.parametrize("text", ["/commands", "/comandos"])
@pytest.mark.asyncio
async def test_every_v1_alias_resolves(text: str) -> None:
    result = await CommandName("commands")(_FakeMessage(text), bot_username="CookieMWbot")
    assert result is not False, f"{text!r} did not resolve to the commands handler"


@pytest.mark.asyncio
async def test_addressed_at_this_bot_resolves() -> None:
    result = await CommandName("commands")(
        _FakeMessage("/commands@CookieMWbot"), bot_username="CookieMWbot"
    )
    assert result is not False


@pytest.mark.asyncio
async def test_addressed_at_a_different_bot_does_not_resolve() -> None:
    result = await CommandName("commands")(
        _FakeMessage("/commands@SomeOtherBot"), bot_username="CookieMWbot"
    )
    assert result is False


@pytest.mark.asyncio
async def test_unrelated_command_does_not_resolve() -> None:
    result = await CommandName("commands")(_FakeMessage("/isalive"), bot_username="CookieMWbot")
    assert result is False


# ------------------------------------------------------------- catalog filtering

_SEEDED_ROW = {"command": "commands", "enabled": True}  # 0001_initial_schema.py:487


class TestCommandAvailableForTenant:
    def test_seeded_row_and_fallback_tenant_is_available(self) -> None:
        """The exact single-tenant shape a fresh deployment ships with: the
        'cookiebot' tenant carries no `disabled_commands`, and the catalog row
        seeds `enabled = true`. This must be True, or /commands stops answering
        on a plain install with no per-tenant configuration at all — v1 parity."""
        assert command_available_for_tenant(_SEEDED_ROW, FALLBACK) is True

    def test_no_catalog_row_is_unavailable(self) -> None:
        """A command absent from the reference table does not exist for anyone."""
        assert command_available_for_tenant(None, FALLBACK) is False

    def test_catalog_disabled_is_unavailable_even_for_an_unrestricted_tenant(self) -> None:
        row = {"command": "commands", "enabled": False}
        tenant = Tenant(tenant_id="cookiebot", display_name="Cookiebot")
        assert command_available_for_tenant(row, tenant) is False

    def test_tenant_disabled_commands_overrides_an_enabled_catalog_row(self) -> None:
        tenant = Tenant(
            tenant_id="acme",
            display_name="Acme",
            disabled_commands=frozenset({"commands"}),
        )
        assert command_available_for_tenant(_SEEDED_ROW, tenant) is False

    def test_tenant_disabling_an_unrelated_command_does_not_affect_this_one(self) -> None:
        tenant = Tenant(
            tenant_id="acme",
            display_name="Acme",
            disabled_commands=frozenset({"dice", "youtube"}),
        )
        assert command_available_for_tenant(_SEEDED_ROW, tenant) is True


class TestCommandsAvailableFailsOpen:
    """A catalog/tenant-registry outage must not hide the help text (AGENTS.md
    §2.6, extended from analytics to this non-critical read)."""

    @pytest.mark.asyncio
    async def test_catalog_read_failure_fails_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from cb_gateway.handlers import listcommand

        async def _boom(command: str) -> Mapping[str, object] | None:
            raise RuntimeError("pg pool not initialised")

        monkeypatch.setattr(listcommand, "_fetch_catalog_row", _boom)

        assert await _commands_available("cookiebot") is True

    @pytest.mark.asyncio
    async def test_no_database_at_all_fails_open(self) -> None:
        """No monkeypatching: in a process with no pg pool (the shape of the
        acceptance-suite environment), the catalog read raises naturally and the
        gate still fails open."""
        assert await _commands_available("cookiebot") is True
