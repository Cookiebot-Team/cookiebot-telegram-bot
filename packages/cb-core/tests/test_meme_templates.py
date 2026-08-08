"""`cb_core.meme_templates` — v1's selection rules, against v1's own CSV.

The catalog is a byte-for-byte copy of `Bot/Static/Meme/meme_metadata.csv`, so
these assert the *rules* v1 applies to it (`SocialContent.py:234-243`) rather
than restating its contents.
"""

from __future__ import annotations

import random

import pytest

from cb_core import meme_templates as templates


def test_the_catalog_loaded() -> None:
    assert len(templates.all_templates()) > 500


def test_zero_blob_templates_are_excluded() -> None:
    """A template with no green rectangle has nowhere to paste a face, and
    v1's own widening loop could never select one either."""
    assert all(t.blob_count >= 1 for t in templates.all_templates())


def test_every_template_declares_as_many_rects_as_blobs() -> None:
    assert all(len(t.blob_rects) == t.blob_count for t in templates.all_templates())


@pytest.mark.parametrize(
    ("lang", "expected"),
    [
        ("pt", templates.PORTUGUESE),
        ("pt-BR", templates.PORTUGUESE),
        ("PT", templates.PORTUGUESE),
        ("en", templates.ENGLISH),
        # v1: `'Portuguese' if 'pt' in language.lower() else 'English'`, so a
        # Spanish group gets the English pool. Preserved (`:234`).
        ("es", templates.ENGLISH),
    ],
)
def test_language_maps_the_way_v1_maps_it(lang: str, expected: str) -> None:
    assert templates.metadata_language(lang) == expected


def test_selection_widens_upward_from_the_tag_count() -> None:
    """v1: `for blob_count in range(len(members_tagged), 6)` (`:236`) — a
    template that seats *more* people than were tagged is still eligible."""
    for template in templates.suitable(3, "en"):
        assert template.blob_count >= 3
    assert len(templates.suitable(3, "en")) > len(templates.suitable(5, "en"))


def test_no_tags_still_selects_from_the_whole_pool() -> None:
    assert templates.suitable(0, "en") == templates.suitable(1, "en")


def test_portuguese_falls_back_to_english_but_not_the_reverse() -> None:
    """v1 has exactly one fallback direction (`:240-243`): the caption text is
    baked into the image, so an English group must never be shown a
    Portuguese template."""
    assert all(t.language == templates.ENGLISH for t in templates.suitable(5, "en"))
    # Portuguese has templates at every count in this catalog, so force the
    # fallback by asking for more than any of them seats.
    assert templates.suitable(templates.MAX_BLOBS + 1, "pt") == ()
    assert templates._collect(templates.PORTUGUESE, 6) == ()  # noqa: SLF001 - the fallback's input


def test_choose_is_deterministic_under_a_seeded_rng() -> None:
    first = templates.choose(2, "en", random.Random(7))
    second = templates.choose(2, "en", random.Random(7))
    assert first is not None
    assert first == second


def test_choose_returns_none_when_nothing_fits() -> None:
    """v1 has no such branch — `contours_green` is read outside the `if` that
    assigns it (`:244-248`), so an empty pool is a NameError (D-ME-1)."""
    assert templates.choose(templates.MAX_BLOBS + 1, "en") is None


def test_storage_key_is_language_and_filename() -> None:
    template = templates.all_templates()[0]
    assert template.storage_key == (
        f"{templates.KEY_PREFIX}/{template.language}/{template.filename}"
    )
