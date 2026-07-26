"""core_listcommand's per-tenant filtering against a real Citus database.

Exercises the two reference-table reads `cb_gateway.handlers.listcommand` makes
(`command_catalog`, `tenants`) and proves the contract in
docs/contracts/core_listcommand.md: a single-tenant deployment with the seeded
defaults resolves `/commands` as always-available (v1 parity), a tenant can opt a
command out (`tenants.disabled_commands`), the catalog's own `enabled` flag is a
global kill switch above any tenant, and — the finding this port exists to
record — the group-level `functions_fun`/`functions_utility` gates have no effect
on this at all, matching v1's unconditional dispatch (`COOKIEBOT.py:276-277`).
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Iterator
from types import ModuleType
from typing import Any

import pytest

from cb_core import group_config, tenancy
from cb_gateway.handlers import listcommand
from qa.integration.factories import World

pytestmark = pytest.mark.integration

Run = Callable[[Coroutine[Any, Any, Any]], Any]


class TestCatalogReferenceTableRead:
    def test_seeded_commands_row_is_enabled(self, run: Run, pg: ModuleType) -> None:
        """0001_initial_schema.py:487 seeds ('commands', 'core', false, NULL,
        'core_listcommand') — enabled takes its SQL default, true."""
        row = run(listcommand._fetch_catalog_row("commands"))  # noqa: SLF001

        assert row is not None
        assert row["command"] == "commands"
        assert row["enabled"] is True

    def test_unknown_command_has_no_row(self, run: Run, pg: ModuleType) -> None:
        row = run(listcommand._fetch_catalog_row("not-a-real-command"))  # noqa: SLF001

        assert row is None


class TestSingleTenantParity:
    def test_default_tenant_can_always_use_commands(self, run: Run, pg: ModuleType) -> None:
        """The exact shape a fresh deployment ships with: 'cookiebot' tenant, no
        `disabled_commands`. Must be True or /commands stops answering with zero
        per-tenant configuration — the acceptance criterion for this whole port."""
        tenant = run(tenancy.registry.by_id("cookiebot"))
        row = run(listcommand._fetch_catalog_row("commands"))  # noqa: SLF001

        assert listcommand.command_available_for_tenant(row, tenant) is True

    def test_functions_gates_off_does_not_hide_the_command_list(
        self, run: Run, world: World
    ) -> None:
        """v1's fun/utility gates refuse *those* commands (`notify_fun_off` /
        `notify_utility_off`, COOKIEBOT.py:218-219,252-253) but never touch
        `/commands`, which is dispatched from its own unconditional elif arm
        (COOKIEBOT.py:276-277). Confirming both gates off still leaves the
        catalog-based availability check (which never reads group_configs at
        all) returning True is the regression test for that finding.
        """
        world.set_config(functions_fun=False, functions_utility=False)
        config = run(group_config.get_config(world.group_id))
        assert config.functions_fun is False
        assert config.functions_utility is False

        tenant = run(tenancy.registry.by_id("cookiebot"))
        row = run(listcommand._fetch_catalog_row("commands"))  # noqa: SLF001

        assert listcommand.command_available_for_tenant(row, tenant) is True


class TestPerTenantFiltering:
    """v2's addition over v1: a brand on the shared 'core' handler pack can opt a
    command out via `tenants.disabled_commands` without a code change."""

    @pytest.fixture
    def restricted_tenant(self, run: Run, pg: ModuleType) -> Iterator[str]:
        tenant_id = "qa-restricted-tenant"
        # TenantRegistry (cb_core/tenancy.py) is a process-wide L1 cache shared by
        # every test in this session — forget() before *and* after so a stale
        # in-memory copy from an earlier or later test never leaks in.
        tenancy.registry.forget(tenant_id)
        run(
            pg.execute(
                """
                INSERT INTO tenants (tenant_id, display_name, disabled_commands)
                VALUES ($1, 'QA Restricted', ARRAY['commands'])
                ON CONFLICT (tenant_id) DO UPDATE SET disabled_commands = EXCLUDED.disabled_commands
                """,
                tenant_id,
                name="test_seed_restricted_tenant",
            )
        )
        yield tenant_id
        run(
            pg.execute(
                "DELETE FROM tenants WHERE tenant_id = $1", tenant_id, name="test_drop_tenant"
            )
        )
        tenancy.registry.forget(tenant_id)

    def test_tenant_disabled_commands_hides_it(self, run: Run, restricted_tenant: str) -> None:
        tenant = run(tenancy.registry.by_id(restricted_tenant))
        row = run(listcommand._fetch_catalog_row("commands"))  # noqa: SLF001

        assert listcommand.command_available_for_tenant(row, tenant) is False

    def test_tenant_disabled_commands_does_not_affect_a_different_tenant(
        self, run: Run, restricted_tenant: str
    ) -> None:
        unrestricted = run(tenancy.registry.by_id("cookiebot"))
        row = run(listcommand._fetch_catalog_row("commands"))  # noqa: SLF001

        assert listcommand.command_available_for_tenant(row, unrestricted) is True


class TestCatalogKillSwitch:
    def test_catalog_disabled_hides_it_for_every_tenant(self, run: Run, pg: ModuleType) -> None:
        """`command_catalog.enabled = false` is the global kill switch — above
        and before any per-tenant opt-out. Restores the seeded value afterwards:
        this row is shared, replicated, global reference data, not scoped to a
        disposable `world` group."""
        run(
            pg.execute(
                "UPDATE command_catalog SET enabled = false WHERE command = 'commands'",
                name="test_disable_commands_catalog_row",
            )
        )
        try:
            tenant = run(tenancy.registry.by_id("cookiebot"))
            row = run(listcommand._fetch_catalog_row("commands"))  # noqa: SLF001

            assert listcommand.command_available_for_tenant(row, tenant) is False
        finally:
            run(
                pg.execute(
                    "UPDATE command_catalog SET enabled = true WHERE command = 'commands'",
                    name="test_restore_commands_catalog_row",
                )
            )
