"""Unit tests for x_drawing_idea's one piece of pure logic: the indexed draw
whose index is what the caption prints.

The send path (gate, storage read, reply_photo) against mock Telegram lives in
`qa/test_x_drawing_idea.py`.
"""

from __future__ import annotations

import random

from cb_core.legacy_assets import LegacyAsset
from cb_gateway.handlers import drawing_idea as di


def _asset(name: str) -> LegacyAsset:
    return LegacyAsset(
        source_path=f"IdeiaDesenho/{name}.png",
        destination_key=f"legacy/v1-bucket/aa/{name}.png",
        byte_size=3,
        content_hash=name,
    )


_POOL = tuple(_asset(str(index)) for index in range(10))


class TestPickReference:
    def test_returns_the_index_it_drew(self) -> None:
        """The index *is* the caption's "Reference ID" (`Miscellaneous.py:139`),
        so it has to come back out of the draw, not be re-derived."""
        picked = di.pick_reference(_POOL, rng=random.Random(1))
        assert picked is not None
        index, entry = picked
        assert _POOL[index] is entry

    def test_is_reproducible_under_a_seeded_rng(self) -> None:
        first = di.pick_reference(_POOL, rng=random.Random(42))
        second = di.pick_reference(_POOL, rng=random.Random(42))
        assert first == second

    def test_can_draw_the_first_and_the_last_row(self) -> None:
        """v1's `randint(0, len-1)` is inclusive at both ends; a slip to
        `randrange`-style bounds would silently make the last reference
        undrawable."""
        drawn = {
            di.pick_reference(_POOL, rng=random.Random(seed))[0]  # type: ignore[index]
            for seed in range(200)
        }
        assert 0 in drawn
        assert len(_POOL) - 1 in drawn

    def test_empty_pool_is_none(self) -> None:
        """v1 raised `ValueError` inside `random.randint(0, -1)`."""
        assert di.pick_reference(()) is None


class TestTheRealPool:
    def test_the_exported_catalog_is_present_and_sorted(self) -> None:
        """The caption's id is a position in this list, so its order is the
        contract (module docstring). `legacy-catalog` sorts by `source_path`;
        this asserts the shipped file actually is."""
        from cb_core import legacy_assets

        entries = legacy_assets.entries_for("IdeiaDesenho")
        assert len(entries) > 3000
        assert [entry.source_path for entry in entries] == sorted(
            entry.source_path for entry in entries
        )
