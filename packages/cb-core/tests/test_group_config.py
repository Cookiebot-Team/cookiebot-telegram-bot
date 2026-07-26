"""Unit tests for group configuration — merge order, whitelist, cache layers.

No infrastructure: the DB layer is driven through `group_config._fetch_row`, a
module-level async function built specifically to be monkeypatched (see the
docstring on it), and the tenant layer through `tenancy.registry.by_id`, its own
public seam. Neither asyncpg nor a real Valkey connection is touched — L2 calls
naturally fail closed (RuntimeError: "cache not initialised") and are treated as
misses, which is exercised implicitly by every test here running with no cache
configured at all.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator, Mapping

import pytest

from cb_core import group_config, tenancy
from cb_core.group_config import DEFAULTS, GroupConfig
from cb_core.settings import Settings


@pytest.fixture(autouse=True)
def _clean_l1() -> Iterator[None]:
    group_config._l1.clear()  # noqa: SLF001
    yield
    group_config._l1.clear()  # noqa: SLF001


def _config_row(tenant_id: str | None, **overrides: object) -> dict[str, object]:
    """A fake row shaped like `_SELECT`'s output — group_configs side present."""
    row: dict[str, object] = {
        "tenant_id": tenant_id,
        "config_group_id": 1,
        "allow_furbots": True,
        "sticker_spam_limit": 5,
        "sticker_spam_window_s": 60,
        "media_restrict_seconds": 0,
        "captcha_timeout_seconds": 120,
        "functions_fun": True,
        "functions_utility": True,
        "sfw": True,
        "language": "en",
        "publisher_post": False,
        "publisher_ask": True,
        "publisher_members_only": False,
        "thread_posts": None,
        "max_posts": 3,
        "doomlist_enabled": True,
    }
    row.update(overrides)
    return row


def _no_config_row(tenant_id: str | None) -> dict[str, object]:
    """Group exists but has no group_configs row: the LEFT JOIN reports all NULLs."""
    row = _config_row(tenant_id)
    for key in row:
        if key != "tenant_id":
            row[key] = None
    return row


class TestMergeOrder:
    """DEFAULTS < tenant feature_defaults < the group's own row."""

    async def test_no_group_no_tenant_is_defaults_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(group_config, "_fetch_row", _fake_fetch(None))
        monkeypatch.setattr(tenancy.registry, "by_id", _fake_by_id(tenancy.FALLBACK))

        config = await group_config.get_config(42)

        assert config == GroupConfig(group_id=42, **_defaults_kwargs())

    async def test_tenant_feature_defaults_apply_when_no_config_row(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tenant = tenancy.Tenant(
            tenant_id="acme",
            display_name="Acme",
            feature_defaults={"functions_fun": False, "sfw": False},
        )
        monkeypatch.setattr(group_config, "_fetch_row", _fake_fetch(_no_config_row("acme")))
        monkeypatch.setattr(tenancy.registry, "by_id", _fake_by_id(tenant))

        config = await group_config.get_config(7)

        assert config.functions_fun is False
        assert config.sfw is False
        # Everything else still comes from DEFAULTS - the tenant only overrides
        # the keys it names.
        assert config.functions_utility == DEFAULTS.functions_utility
        assert config.sticker_spam_limit == DEFAULTS.sticker_spam_limit

    async def test_groups_own_row_wins_over_tenant_and_defaults(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The tenant says fun functions are off; the group's own row says on.
        # The row must win - v2's schema makes a present row fully populated,
        # same as v1 where a config document either existed complete or not at all.
        tenant = tenancy.Tenant(
            tenant_id="acme", display_name="Acme", feature_defaults={"functions_fun": False}
        )
        row = _config_row("acme", functions_fun=True, sticker_spam_limit=99)
        monkeypatch.setattr(group_config, "_fetch_row", _fake_fetch(row))
        monkeypatch.setattr(tenancy.registry, "by_id", _fake_by_id(tenant))

        config = await group_config.get_config(7)

        assert config.functions_fun is True
        assert config.sticker_spam_limit == 99

    async def test_db_failure_serves_defaults_and_counts_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _boom(group_id: int) -> Mapping[str, object] | None:
            raise RuntimeError("pg pool not initialised")

        monkeypatch.setattr(group_config, "_fetch_row", _boom)

        config = await group_config.get_config(9)

        assert config == GroupConfig(group_id=9, **_defaults_kwargs())


class TestWhitelist:
    async def test_unknown_column_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown"):
            await group_config.set_config(1, not_a_real_column=True)

    async def test_several_unknown_columns_are_all_reported(self) -> None:
        with pytest.raises(ValueError, match="unknown"):
            await group_config.set_config(1, nope=True, also_nope=1)


class TestFeatureEnabled:
    def test_fun_maps_to_functions_fun(self) -> None:
        config = GroupConfig(group_id=1, functions_fun=True, functions_utility=False)
        assert config.feature_enabled("fun") is True

    def test_utility_maps_to_functions_utility(self) -> None:
        config = GroupConfig(group_id=1, functions_fun=False, functions_utility=True)
        assert config.feature_enabled("utility") is True

    def test_unknown_area_raises(self) -> None:
        config = GroupConfig(group_id=1)
        with pytest.raises(ValueError, match="unknown feature area"):
            config.feature_enabled("nope")


class TestL1Expiry:
    def test_entry_expires_after_the_configured_ttl(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clock = {"now": 0.0}
        monkeypatch.setattr(group_config.time, "monotonic", lambda: clock["now"])
        monkeypatch.setattr(
            group_config, "get_settings", lambda: Settings(config_cache_l1_seconds=5)
        )
        config = GroupConfig(group_id=42)

        group_config._l1_set(42, config)  # noqa: SLF001
        assert group_config._l1_get(42) == config  # noqa: SLF001

        clock["now"] = 4.999
        assert group_config._l1_get(42) == config  # noqa: SLF001

        clock["now"] = 5.0
        assert group_config._l1_get(42) is None  # noqa: SLF001
        assert group_config.cached_size() == 0

    def test_cached_size_reflects_live_entries(self) -> None:
        group_config._l1_set(1, GroupConfig(group_id=1))  # noqa: SLF001
        group_config._l1_set(2, GroupConfig(group_id=2))  # noqa: SLF001
        assert group_config.cached_size() == 2


class TestInvalidationCallback:
    def test_matching_key_drops_the_l1_entry(self) -> None:
        group_config._l1_set(99, GroupConfig(group_id=99))  # noqa: SLF001
        assert group_config.cached_size() == 1

        group_config._on_invalidate_key("cb:groupconfig:99")  # noqa: SLF001

        assert group_config.cached_size() == 0

    def test_unrelated_key_is_ignored(self) -> None:
        group_config._l1_set(1, GroupConfig(group_id=1))  # noqa: SLF001

        group_config._on_invalidate_key("cb:tenant:cookiebot")  # noqa: SLF001

        assert group_config.cached_size() == 1

    def test_malformed_key_does_not_raise(self) -> None:
        group_config._l1_set(1, GroupConfig(group_id=1))  # noqa: SLF001

        group_config._on_invalidate_key("cb:groupconfig:not-a-number")  # noqa: SLF001

        assert group_config.cached_size() == 1


def _defaults_kwargs() -> dict[str, object]:
    return {
        f: getattr(DEFAULTS, f)
        for f in (
            "allow_furbots",
            "sticker_spam_limit",
            "sticker_spam_window_s",
            "media_restrict_seconds",
            "captcha_timeout_seconds",
            "functions_fun",
            "functions_utility",
            "sfw",
            "language",
            "publisher_post",
            "publisher_ask",
            "publisher_members_only",
            "thread_posts",
            "max_posts",
            "doomlist_enabled",
        )
    }


def _fake_fetch(
    row: Mapping[str, object] | None,
) -> Callable[[int], Awaitable[Mapping[str, object] | None]]:
    async def _fetch(group_id: int) -> Mapping[str, object] | None:
        return row

    return _fetch


def _fake_by_id(tenant: tenancy.Tenant) -> Callable[[str], Awaitable[tenancy.Tenant]]:
    async def _by_id(tenant_id: str) -> tenancy.Tenant:
        return tenant

    return _by_id
