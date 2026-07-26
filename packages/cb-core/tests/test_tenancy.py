"""Tenant model.

Pure logic only — the registry's database path is covered at the integration
layer. What matters here is that a tenant with no configuration behaves exactly
like today's single-brand deployment, so adding tenancy cannot change behaviour
for the bot that is already running.
"""

from __future__ import annotations

from cb_core.tenancy import DEFAULT_TENANT, FALLBACK, Tenant


class TestFallback:
    def test_single_brand_deployments_need_no_configuration(self) -> None:
        assert FALLBACK.tenant_id == DEFAULT_TENANT
        assert FALLBACK.handler_pack == "core"
        assert FALLBACK.active

    def test_fallback_disables_nothing(self) -> None:
        assert FALLBACK.command_enabled("isalive")
        assert FALLBACK.command_enabled("anything-at-all")

    def test_fallback_has_no_owners(self) -> None:
        """No owner rows means no one passes an owner check by accident."""
        assert not FALLBACK.owns(12345)


class TestTenant:
    def test_owner_check(self) -> None:
        tenant = Tenant(tenant_id="t", display_name="T", owner_ids=(1, 2))
        assert tenant.owns(1)
        assert not tenant.owns(3)

    def test_disabled_commands(self) -> None:
        tenant = Tenant(
            tenant_id="t", display_name="T", disabled_commands=frozenset({"meme", "ship"})
        )
        assert not tenant.command_enabled("meme")
        assert tenant.command_enabled("dice")

    def test_defaults_are_conservative(self) -> None:
        tenant = Tenant(tenant_id="t", display_name="T")
        assert tenant.handler_pack == "core"
        assert tenant.default_locale == "en"
        assert tenant.monthly_llm_budget_usd is None
        assert tenant.storage_prefix == ""
