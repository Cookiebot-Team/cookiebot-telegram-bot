"""Unit coverage for fun_ship — trigger surface, argument rules, rendered text.

Pure logic only (no dispatcher, no Telegram, no DB). The rules asserted here are
v1's, including the two that read like bugs and are not fixed: a single argument
is discarded rather than used, and an `@` the user typed is not stripped before
the catalog string adds its own. Both are argued in
`cb_gateway/handlers/ship.py`'s module docstring and
`docs/contracts/fun_ship.md`; the end-to-end version lives in
`qa/features/fun_ship.feature`.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

import pytest

from cb_core import locales
from cb_core.textmatch import ParsedCommand, parse_command
from cb_gateway.filters import CommandName
from cb_gateway.handlers.ship import explicit_targets, render

BOT = "CookieMWbot"


def _parsed(text: str) -> ParsedCommand:
    parsed = parse_command(text, BOT)
    assert parsed is not None, f"{text!r} did not parse as a command at all"
    return parsed


@dataclass
class _FakeMessage:
    text: str | None


# --------------------------------------------------------------------- triggers


@pytest.mark.parametrize("text", ["/ship", "/shipp", "/shippar", "/SHIP", "/ship @a @b"])
@pytest.mark.asyncio
async def test_every_v1_trigger_resolves(text: str) -> None:
    """`/shippar` and `/ship` are v1's (COOKIEBOT.py:232); `/shipp` is QA's
    spelling. AGENTS.md §2.1: all three must keep resolving, forever."""
    result = await CommandName("ship")(_FakeMessage(text), bot_username=BOT)
    assert result is not False, f"{text!r} did not resolve to the ship command"
    assert isinstance(result, dict)
    assert result["parsed"].name == "ship"


@pytest.mark.asyncio
async def test_addressed_at_another_bot_does_not_resolve() -> None:
    result = await CommandName("ship")(_FakeMessage("/ship@SomeOtherBot"), bot_username=BOT)
    assert result is False


# -------------------------------------------------------------------- arguments


def test_two_arguments_are_used_verbatim() -> None:
    assert explicit_targets(_parsed("/ship alice bob")) == ("alice", "bob")


def test_a_single_argument_is_discarded() -> None:
    """v1: `len(msg['text'].split()) >= 3` — "/shipp @user1" is two tokens, so
    the branch never runs and *both* targets come from the random pool. QA's
    second scenario expects the opposite; v1 wins (AGENTS.md §1)."""
    assert explicit_targets(_parsed("/ship @user1")) is None


def test_no_arguments_is_the_random_path() -> None:
    assert explicit_targets(_parsed("/ship")) is None


def test_extra_arguments_beyond_the_second_are_ignored() -> None:
    """v1 indexes positionally (`split()[1]`, `split()[2]`) and never looks further."""
    assert explicit_targets(_parsed("/ship alice bob carol")) == ("alice", "bob")


def test_typed_at_signs_are_not_stripped() -> None:
    """The catalog string supplies its own `@`, so v1 really does render
    `@@alice`. Preserved — see the handler docstring."""
    assert explicit_targets(_parsed("/ship @alice @bob")) == ("@alice", "@bob")
    assert "@@alice" in render("@alice", "@bob", "en")


# ---------------------------------------------------------------------- render


def test_render_uses_the_v1_catalog_string() -> None:
    text = render("alice", "bob", "en")
    assert "@alice" in text
    assert "@bob" in text
    assert text.startswith(locales.get("ship", "en").split("@")[0])


@pytest.mark.parametrize("lang", ["en", "pt", "es"])
def test_render_resolves_in_every_v1_language(lang: str) -> None:
    text = render("alice", "bob", lang)
    assert "%(" not in text, f"unsubstituted placeholder in {lang}: {text!r}"
    assert "@alice" in text and "@bob" in text


@pytest.mark.parametrize("lang", ["en", "pt", "es"])
def test_ship_dynamic_comes_from_the_v1_pool(lang: str) -> None:
    pool = locales.lines("ship_dynamics", lang)
    assert pool, f"{lang} has no ship_dynamics lines"
    random.seed(7)
    text = render("alice", "bob", lang)
    assert any(line in text for line in pool)


def test_divorce_probability_stays_within_v1_bounds() -> None:
    """`random.randint(0, 100)` — inclusive at both ends, never a float."""
    seen: set[int] = set()
    for seed in range(300):
        random.seed(seed)
        text = render("alice", "bob", "en")
        percent = text.rsplit("Chance of divorce: ", 1)[1].split("%", 1)[0]
        value = int(percent)
        assert 0 <= value <= 100
        seen.add(value)
    assert len(seen) > 1, "divorce probability never varied — is it still random?"


def test_children_quantity_is_one_of_v1s_four_options() -> None:
    for seed in range(200):
        random.seed(seed)
        text = render("alice", "bob", "en")
        children = text.rsplit("Children: ", 1)[1].split(" ", 1)[0]
        assert children in {"0", "1", "2", "3"}
