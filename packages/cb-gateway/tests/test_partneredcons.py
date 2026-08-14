"""Unit tests for fun_partneredcons' pure logic: the countdown maths, the
wraparound, the happening-now window and the caption templates.

The send path (pool pick, storage read, reply_photo) against mock Telegram
lives in `qa/test_fun_partneredcons.py`. Model:
`packages/cb-gateway/tests/test_battle.py`.
"""

from __future__ import annotations

import datetime as dt

import pytest

from cb_gateway.handlers import partneredcons as pc

_PATAS = pc._BY_COMMAND["con_patas"]  # noqa: SLF001 - the table is the fixture
_PAWSTRAL = pc._BY_COMMAND["con_pawstral"]  # noqa: SLF001
_TREX = pc._BY_COMMAND["con_trex"]  # noqa: SLF001


class TestDaysRemaining:
    def test_counts_v1s_extra_day(self) -> None:
        """v1: `(target - now).days + 1` (`Miscellaneous.py:268`) — the day
        before the event reads 2, not 1."""
        assert pc.days_remaining((11, 12, 2026), dt.datetime(2026, 12, 10)) == 2
        assert pc.days_remaining((11, 12, 2026), dt.datetime(2026, 12, 11)) == 1

    def test_within_five_days_past_stays_negative(self) -> None:
        assert pc.days_remaining((11, 12, 2026), dt.datetime(2026, 12, 16)) == -4

    def test_more_than_five_days_past_wraps_by_365(self) -> None:
        """The quirk, preserved: a passed date walks forward in 365-day hops
        (never 366), so the count is "days until the same calendar date, N
        hops away" and drifts a day every four years."""
        assert pc.days_remaining((21, 11, 2025), dt.datetime(2026, 8, 13)) == 101

    def test_wraps_repeatedly_for_a_long_dead_date(self) -> None:
        remaining = pc.days_remaining((1, 1, 2020), dt.datetime(2026, 8, 13))
        assert remaining >= -5 or remaining > 0
        assert remaining > -6


class TestHappeningNow:
    @pytest.mark.parametrize("remaining", [0, -1, -5])
    def test_inside_the_window(self, remaining: int) -> None:
        assert pc.is_happening_now(remaining) is True

    @pytest.mark.parametrize("remaining", [1, -6, 200])
    def test_outside_the_window(self, remaining: int) -> None:
        assert pc.is_happening_now(remaining) is False


class TestCaptionFor:
    def test_countdown_caption_carries_keycap_digits_and_the_hardcoded_dates(self) -> None:
        caption = pc.caption_for(_PATAS, dt.datetime(2026, 12, 9), "en")
        assert caption is not None
        assert "3️⃣" in caption
        # v1 prints the hardcoded day/month, never a wrapped-forward one.
        assert "📆 11 a 14/12" in caption

    def test_happening_now_replaces_the_whole_caption_with_the_youtube_link(self) -> None:
        caption = pc.caption_for(_PATAS, dt.datetime(2026, 12, 13), "en")
        assert caption == pc._HAPPENING_NOW  # noqa: SLF001

    def test_a_spanish_group_still_gets_the_portuguese_caption(self) -> None:
        """v1's caption is an f-string, not a catalog entry — only the `cta`
        line is looked up, and only `en` carries the `event` object. A
        Spanish group reads Portuguese here, and that is the ported
        behaviour (module docstring)."""
        caption = pc.caption_for(_PATAS, dt.datetime(2026, 12, 9), "es")
        assert caption is not None
        assert "Faltam" in caption

    def test_pawstral_is_english_for_every_language(self) -> None:
        caption = pc.caption_for(_PAWSTRAL, dt.datetime(2025, 8, 1), "pt")
        assert caption is not None
        assert "days left until Pawstral" in caption

    def test_trex_has_no_caption_at_all(self) -> None:
        """Net-new trigger, no date anywhere to count down to — QA asks only
        for a picture (module docstring)."""
        assert pc.caption_for(_TREX, dt.datetime(2026, 8, 13), "en") is None


class TestEventTable:
    def test_every_command_is_a_canonical_alias_target(self) -> None:
        from cb_core.textmatch import COMMAND_ALIASES

        targets = set(COMMAND_ALIASES.values())
        for event in pc._EVENTS:  # noqa: SLF001
            assert event.command in targets

    def test_every_prefix_has_a_generated_catalog(self) -> None:
        """The pools ship (`cb.py legacy-catalog`), so a typo'd prefix here
        would be a silent "this command sends nothing" in production."""
        from cb_core import legacy_assets

        for event in pc._EVENTS:  # noqa: SLF001
            assert legacy_assets.entries_for(event.prefix), event.prefix
