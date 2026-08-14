"""Unit tests for `cb_core.image_search` — v1's search-term extraction and the
blocklist it vendors byte-for-byte."""

from __future__ import annotations

from cb_core import image_search


class TestBlocklist:
    def test_ships_v1s_49_entries(self) -> None:
        assert len(image_search.AVOID_SEARCH) == 49

    def test_carries_the_shell_paths_it_was_written_for(self) -> None:
        for entry in ("bash", "etc", "usr", "tmp", "proc", "root"):
            assert entry in image_search.AVOID_SEARCH

    def test_carries_the_bare_punctuation_too(self) -> None:
        """The odd-looking half of v1's list: a stray `/@` or `/-` in a group
        would otherwise become an image search."""
        for entry in ("@", "-", "|", "o/"):
            assert entry in image_search.AVOID_SEARCH


class TestSearchTerm:
    def test_leading_slash_becomes_a_space(self) -> None:
        """v1 keeps the leading space (`:148`); Google trims it, and trimming
        it here would be a different string from the one v1 sent."""
        assert image_search.search_term("/french fries") == " french fries"

    def test_every_slash_becomes_a_space(self) -> None:
        assert image_search.search_term("/and/or") == " and or"

    def test_truncates_at_the_first_at_sign(self) -> None:
        assert image_search.search_term("/cat @dog") == " cat "

    def test_a_command_addressed_at_the_bot_loses_the_address(self) -> None:
        assert image_search.search_term("/cat@CookieMWbot") == " cat"


class TestIsAvoided:
    def test_first_word_only(self) -> None:
        assert image_search.is_avoided(image_search.search_term("/etc")) is True
        # "etc" is blocked as a *first* word; further along it is just a word.
        assert image_search.is_avoided(image_search.search_term("/cats etc")) is False

    def test_an_ordinary_query_passes(self) -> None:
        assert image_search.is_avoided(image_search.search_term("/french fries")) is False

    def test_an_empty_term_is_avoided_rather_than_raising(self) -> None:
        """v1 raises `IndexError` on `searchterm.split()[0]` here and the
        dispatcher's bare `except` turns it into silence; this reaches the
        same silence without the traceback."""
        assert image_search.is_avoided(" ") is True
