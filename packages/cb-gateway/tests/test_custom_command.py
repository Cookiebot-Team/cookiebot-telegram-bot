"""Unit tests for x_custom_commands' pure logic and its two filters' matching
rules, plus the pack registry that gates the family.

The send path against mock Telegram lives in `qa/test_x_custom_commands.py`.
"""

from __future__ import annotations

from typing import Any

import pytest
from aiogram.types import Message

from cb_core import legacy_assets, tenancy
from cb_core.legacy_assets import LegacyAsset
from cb_gateway import packs
from cb_gateway.filters import CustomCommandName
from cb_gateway.handlers import custom_command as cc


def _message(text: str) -> Message:
    return Message.model_validate(
        {
            "message_id": 1,
            "date": 0,
            "chat": {"id": -100123, "type": "supergroup", "title": "g"},
            "text": text,
        }
    )


def _pool(count: int) -> tuple[LegacyAsset, ...]:
    return tuple(
        LegacyAsset(
            source_path=f"Custom/louie/{index}.jpg",
            destination_key=f"legacy/v1-bucket/aa/{index}.jpg",
            byte_size=3,
            content_hash=str(index),
        )
        for index in range(count)
    )


class TestParseIndex:
    def test_second_token_when_it_is_all_digits(self) -> None:
        assert cc.parse_index("7") == 7
        assert cc.parse_index("7 and some words") == 7

    def test_no_argument_is_none(self) -> None:
        assert cc.parse_index("") is None

    def test_non_digit_is_none(self) -> None:
        """v1 uses `str.isdigit()`, so a negative or fractional number is not
        an index and falls through to the random draw (`:148-149`)."""
        assert cc.parse_index("-1") is None
        assert cc.parse_index("1.5") is None
        assert cc.parse_index("hello") is None


class TestDisplayName:
    def test_capitalize_not_title(self) -> None:
        """v1: `.capitalize()` (`:153`), which lower-cases the rest."""
        assert cc.display_name("tailslunar") == "Tailslunar"
        assert cc.display_name("mrnatmax") == "Mrnatmax"


class TestCustomCommandNameFilter:
    @pytest.fixture(autouse=True)
    def _one_pool(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            legacy_assets,
            "entries_for_custom",
            lambda name: _pool(3) if name == "louie" else (),
        )

    async def test_matches_a_pool_name_and_injects_it(self) -> None:
        result = await CustomCommandName()(_message("/louie"))
        assert result == {"custom": ("louie", "")}

    async def test_carries_the_arguments(self) -> None:
        result = await CustomCommandName()(_message("/louie 2"))
        assert result == {"custom": ("louie", "2")}

    async def test_is_case_insensitive(self) -> None:
        assert await CustomCommandName()(_message("/LOUIE"))

    async def test_ignores_a_command_with_no_pool(self) -> None:
        assert await CustomCommandName()(_message("/nobody")) is False

    async def test_ignores_plain_text(self) -> None:
        assert await CustomCommandName()(_message("louie")) is False

    async def test_accepts_a_command_addressed_at_this_bot(self) -> None:
        result = await CustomCommandName()(_message("/louie@CookieMWbot"), "CookieMWbot")
        assert result == {"custom": ("louie", "")}

    async def test_ignores_a_command_addressed_at_another_bot(self) -> None:
        """v1 stripped exactly two of its own five usernames and would have
        answered for a third bot's `/louie@SomeOtherBot`; this follows
        `parse_command`'s rule instead (filter docstring)."""
        assert await CustomCommandName()(_message("/louie@OtherBot"), "CookieMWbot") is False


class TestPacks:
    def test_core_provides_the_legacy_family(self) -> None:
        """v1 parity: the Cookiebot brand answers these, and its tenant row
        carries the default pack."""
        assert packs.LEGACY_CUSTOM in packs.families_for("core")

    def test_minimal_provides_nothing(self) -> None:
        assert packs.families_for("minimal") == frozenset()

    def test_an_unknown_pack_falls_back_to_core(self) -> None:
        """A typo in a tenant row must not silently delete commands."""
        assert packs.families_for("typo") == packs.families_for("core")

    async def test_tenant_provides_reads_the_tenant_row(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _minimal(skin: str) -> Any:
            return tenancy.Tenant(tenant_id="brand", display_name="Brand", handler_pack="minimal")

        monkeypatch.setattr(tenancy.registry, "by_skin", _minimal)
        assert await packs.tenant_provides("brand", packs.LEGACY_CUSTOM) is False

    async def test_a_registry_outage_still_provides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`registry.by_skin` never raises — it answers `FALLBACK`, whose pack
        is `core` — so an unreachable database runs the command."""

        async def _fallback(skin: str) -> Any:
            return tenancy.FALLBACK

        monkeypatch.setattr(tenancy.registry, "by_skin", _fallback)
        assert await packs.tenant_provides("brand", packs.LEGACY_CUSTOM) is True


class TestTheRealCatalog:
    def test_no_custom_name_collides_with_a_real_command(self) -> None:
        """The custom router is registered last so a real command always wins,
        but a collision would still mean one of the 53 pools is unreachable —
        worth knowing about rather than discovering in production."""
        from cb_core.textmatch import COMMAND_ALIASES

        assert not set(legacy_assets.custom_command_names()) & set(COMMAND_ALIASES)

    def test_every_advertised_name_has_a_non_empty_pool(self) -> None:
        for name in legacy_assets.custom_command_names():
            assert legacy_assets.entries_for_custom(name), name
