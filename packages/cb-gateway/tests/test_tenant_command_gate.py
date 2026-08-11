"""Unit coverage for `TenantCommandGateMiddleware` — the dispatch-level
enforcement of `tenants.disabled_commands` that `.specs/features/platform_tenancy/spec.md`
named as the one concrete gap in the feature: before this, a "disabled"
command still ran for anyone who typed it, because `disabled_commands` was
only ever consulted while building `/commands`' own listing.

No Telegram objects, no database, no real cache: `data["parsed_command"]` is
constructed directly (that field is what `TelemetryMiddleware` — tested
separately in `test_telemetry.py` — already guarantees is populated before
this middleware runs), and the tenant-registry/catalog seams are monkeypatched
the same way `test_listcommand.py` patches `_fetch_catalog_row`.
"""

from __future__ import annotations

from typing import Any

import pytest

from cb_core.tenancy import FALLBACK, Tenant
from cb_core.textmatch import ParsedCommand
from cb_gateway import middlewares

_SEEDED_ROW = {"command": "commands", "enabled": True}  # 0001_initial_schema.py:487


def _command(name: str, *, args: str = "", target_bot: str = "") -> ParsedCommand:
    return ParsedCommand(name, args, target_bot, f"/{name}")


async def _handler(event: Any, data: dict[str, Any]) -> str:
    return "handled"


class _Unreachable:
    """Fails the test the moment anything calls it — used to prove a lookup
    seam was never touched for a non-command update."""

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("lookup seam must not be called for a non-command update")


class TestNonCommandsPassThroughUntouched:
    async def test_no_parsed_command_skips_every_lookup(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(middlewares, "fetch_catalog_row", _Unreachable())
        monkeypatch.setattr(middlewares.tenancy.registry, "by_skin", _Unreachable())

        data: dict[str, Any] = {"skin": "cookiebot", "parsed_command": None}
        result = await middlewares.TenantCommandGateMiddleware()(_handler, object(), data)

        assert result == "handled"

    async def test_missing_parsed_command_key_is_treated_the_same_as_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A plain message never has `parsed_command` set by anything upstream
        # of TelemetryMiddleware in a real pipeline, but `.get` must default
        # safely regardless of whether the key is absent or explicitly None.
        monkeypatch.setattr(middlewares, "fetch_catalog_row", _Unreachable())
        monkeypatch.setattr(middlewares.tenancy.registry, "by_skin", _Unreachable())

        data: dict[str, Any] = {"skin": "cookiebot"}
        result = await middlewares.TenantCommandGateMiddleware()(_handler, object(), data)

        assert result == "handled"


class TestEnabledCommandPasses:
    async def test_seeded_tenant_and_catalog_row_runs_the_handler(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_by_skin(skin: str) -> Tenant:
            return FALLBACK

        async def fake_fetch(command: str) -> dict[str, Any]:
            assert command == "commands"
            return _SEEDED_ROW

        monkeypatch.setattr(middlewares.tenancy.registry, "by_skin", fake_by_skin)
        monkeypatch.setattr(middlewares, "fetch_catalog_row", fake_fetch)

        data: dict[str, Any] = {"skin": "cookiebot", "parsed_command": _command("commands")}
        result = await middlewares.TenantCommandGateMiddleware()(_handler, object(), data)

        assert result == "handled"


class TestDisabledCommandIsDropped:
    async def test_tenant_disabled_command_is_silently_dropped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tenant = Tenant(
            tenant_id="acme", display_name="Acme", disabled_commands=frozenset({"dice"})
        )

        async def fake_by_skin(skin: str) -> Tenant:
            return tenant

        async def fake_fetch(command: str) -> dict[str, Any]:
            return {"command": "dice", "enabled": True}

        monkeypatch.setattr(middlewares.tenancy.registry, "by_skin", fake_by_skin)
        monkeypatch.setattr(middlewares, "fetch_catalog_row", fake_fetch)

        counter = middlewares.metrics.updates_dropped_total.labels(reason="tenant_disabled")
        before = counter._value.get()  # noqa: SLF001

        data: dict[str, Any] = {"skin": "acme", "parsed_command": _command("dice")}
        result = await middlewares.TenantCommandGateMiddleware()(_handler, object(), data)

        assert result is None
        after = counter._value.get()  # noqa: SLF001
        assert after == before + 1

    async def test_command_absent_from_the_catalog_matches_the_shared_predicate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`command_available_for_tenant(None, tenant)` is `False` regardless of
        tenant (`command_catalog.py`'s docstring) — a command missing from the
        reference table does not exist for anyone. This proves the middleware
        reduces to that same predicate rather than inventing its own rule."""

        async def fake_by_skin(skin: str) -> Tenant:
            return FALLBACK

        async def fake_fetch(command: str) -> None:
            return None

        monkeypatch.setattr(middlewares.tenancy.registry, "by_skin", fake_by_skin)
        monkeypatch.setattr(middlewares, "fetch_catalog_row", fake_fetch)

        data: dict[str, Any] = {"skin": "cookiebot", "parsed_command": _command("nosuchcommand")}
        result = await middlewares.TenantCommandGateMiddleware()(_handler, object(), data)

        assert result is None


class TestFailsOpen:
    async def test_catalog_lookup_raising_still_runs_the_handler(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_by_skin(skin: str) -> Tenant:
            return FALLBACK

        async def boom(command: str) -> None:
            raise RuntimeError("pg pool not initialised")

        monkeypatch.setattr(middlewares.tenancy.registry, "by_skin", fake_by_skin)
        monkeypatch.setattr(middlewares, "fetch_catalog_row", boom)

        data: dict[str, Any] = {"skin": "cookiebot", "parsed_command": _command("commands")}
        result = await middlewares.TenantCommandGateMiddleware()(_handler, object(), data)

        assert result == "handled"

    async def test_tenant_registry_raising_still_runs_the_handler(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def boom(skin: str) -> Tenant:
            raise RuntimeError("valkey down")

        async def fake_fetch(command: str) -> dict[str, Any]:
            return _SEEDED_ROW

        monkeypatch.setattr(middlewares.tenancy.registry, "by_skin", boom)
        monkeypatch.setattr(middlewares, "fetch_catalog_row", fake_fetch)

        data: dict[str, Any] = {"skin": "cookiebot", "parsed_command": _command("commands")}
        result = await middlewares.TenantCommandGateMiddleware()(_handler, object(), data)

        assert result == "handled"
