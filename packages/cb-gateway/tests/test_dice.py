"""Unit coverage for fun_dice — trigger surface, argument parsing and bounds.

Pure logic only (no dispatcher, no Telegram, no DB): every trigger shape must
resolve through `CommandName("dice")`, and `parse_invocation`/`render_roll`/
`roll` (the pure functions behind the handler) must reduce to v1's exact
argument-handling rules, including the ones v1 got wrong (see
docs/contracts/fun_dice.md and cb_gateway/handlers/dice.py's module docstring
for the full behaviour contract this file asserts against). The end-to-end
version of the same assertions lives in qa/features/fun_dice.feature and
qa/test_fun_dice.py.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from cb_core.textmatch import ParsedCommand, parse_command
from cb_gateway.filters import CommandName
from cb_gateway.handlers.dice import RollRequest, head_word, parse_invocation, render_roll, roll

BOT = "CookieMWbot"


def _parsed(text: str) -> ParsedCommand:
    parsed = parse_command(text, BOT)
    assert parsed is not None, f"{text!r} did not parse as a command at all"
    return parsed


@dataclass
class _FakeMessage:
    """CommandName only ever reads `.text` off the message it's given."""

    text: str | None


# --------------------------------------------------------------------- triggers


@pytest.mark.parametrize(
    "text",
    ["/dado", "/dice", "/roll", "/roll 6", "/d20", "/d6", "/D20", "/dado 5"],
)
@pytest.mark.asyncio
async def test_every_trigger_shape_resolves(text: str) -> None:
    result = await CommandName("dice")(_FakeMessage(text), bot_username=BOT)
    assert result is not False, f"{text!r} did not resolve to the dice command"
    assert isinstance(result, dict)
    assert result["parsed"].name == "dice"


@pytest.mark.asyncio
async def test_addressed_at_this_bot_resolves() -> None:
    result = await CommandName("dice")(_FakeMessage(f"/dado@{BOT}"), bot_username=BOT)
    assert result is not False


@pytest.mark.asyncio
async def test_addressed_at_a_different_bot_does_not_resolve() -> None:
    result = await CommandName("dice")(_FakeMessage("/dado@SomeOtherBot"), bot_username=BOT)
    assert result is False


@pytest.mark.asyncio
async def test_unrelated_command_does_not_resolve() -> None:
    result = await CommandName("dice")(_FakeMessage("/isalive"), bot_username=BOT)
    assert result is False


@pytest.mark.parametrize("sides", ["6", "20", "9999", "99999", "1000000"])
def test_shorthand_has_no_digit_cap(sides: str) -> None:
    """v1 parses the sides with a bare `int(text.split()[0][2:])`
    (`Miscellaneous.py:172`), so it has no upper bound. `_DICE_SHORTHAND` used to
    cap at 4 digits, which made `/d99999` fail to parse as a command at all — the
    bot answered nothing rather than answering badly. Bounds belong to the
    handler, which replies with v1's usage text for values it will not roll.
    """
    parsed = parse_command(f"/d{sides}", BOT)
    assert parsed is not None
    assert parsed.name == "dice"
    assert parsed.args == sides


# ----------------------------------------------------------------------- head_word


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/dado", "dado"),
        ("/dice", "dice"),
        ("/roll", "roll"),
        ("/roll 6", "roll"),
        ("/d20", "d20"),
        ("/D20", "d20"),
        (f"/dado@{BOT}", "dado"),
        (f"/d20@{BOT} 3", "d20"),
        (f"/ROLL@{BOT} 6", "roll"),
    ],
)
def test_head_word_extraction(raw: str, expected: str) -> None:
    assert head_word(raw) == expected


# ------------------------------------------------------------------ parse_invocation


class TestBareAliasesAlwaysShowTheExample:
    """v1: `start = msg['text'].split(" ")[0]`; `if start in ("/dado", "/dice")`
    — trailing text never changes which branch runs (Miscellaneous.py:163-165).
    """

    @pytest.mark.parametrize(
        "text",
        ["/dado", "/dice", "/dado 5", "/dice 20 3", f"/dado@{BOT}", f"/dado@{BOT} 6"],
    )
    def test_returns_none(self, text: str) -> None:
        assert parse_invocation(_parsed(text)) is None


class TestShorthandForm:
    def test_no_args_defaults_to_one_roll(self) -> None:
        assert parse_invocation(_parsed("/d20")) == RollRequest(sides=20, times=1)

    def test_second_token_is_times(self) -> None:
        assert parse_invocation(_parsed("/d6 5")) == RollRequest(sides=6, times=5)

    def test_times_clamped_to_twenty(self) -> None:
        """v1: `vezes = max(min(20, vezes), 1)` (Miscellaneous.py:171)."""
        assert parse_invocation(_parsed("/d6 25")) == RollRequest(sides=6, times=20)

    def test_times_zero_clamped_to_one(self) -> None:
        assert parse_invocation(_parsed("/d6 0")) == RollRequest(sides=6, times=1)

    def test_times_negative_clamped_to_one(self) -> None:
        assert parse_invocation(_parsed("/d6 -5")) == RollRequest(sides=6, times=1)

    def test_non_numeric_times_falls_back_to_example(self) -> None:
        """v1 would crash here (bare `int(...)`, uncaught, silently swallowed by
        COOKIEBOT.py's top-level except). Fixed: reply with the usage example
        instead of going silent."""
        assert parse_invocation(_parsed("/d6 abc")) is None

    def test_zero_sides_falls_back_to_example(self) -> None:
        """v1: "0".isdigit() is True, so /d0 dispatches and then crashes inside
        random.randint(1, 0). Fixed the same way as non-numeric times."""
        assert parse_invocation(_parsed("/d0")) is None

    def test_extra_arguments_beyond_the_second_are_ignored(self) -> None:
        """v1 only ever reads split()[1] for vezes; anything past that is never
        looked at (Miscellaneous.py:170), not an error. Preserved as-is."""
        assert parse_invocation(_parsed("/d20 5 hello world")) == RollRequest(sides=20, times=5)

    def test_addressed_at_this_bot_still_parses(self) -> None:
        assert parse_invocation(_parsed(f"/d20@{BOT} 3")) == RollRequest(sides=20, times=3)


class TestRollAlias:
    """`roll` has no v1 equivalent (FEATURE-MAP: "spec/code trigger mismatch") —
    a net-new alias gets to define its own sensible semantics, modelled on
    `/d<N>` (see dice.py's module docstring)."""

    def test_bare_roll_shows_the_example(self) -> None:
        """QA's third scenario: "roll" alone -> "an error message indicating
        that the number of sides must be specified"."""
        assert parse_invocation(_parsed("/roll")) is None

    def test_sides_from_first_argument(self) -> None:
        assert parse_invocation(_parsed("/roll 6")) == RollRequest(sides=6, times=1)

    def test_second_argument_is_times(self) -> None:
        assert parse_invocation(_parsed("/roll 6 3")) == RollRequest(sides=6, times=3)

    def test_times_clamped_to_twenty(self) -> None:
        assert parse_invocation(_parsed("/roll 6 99")) == RollRequest(sides=6, times=20)

    def test_times_zero_clamped_to_one(self) -> None:
        assert parse_invocation(_parsed("/roll 6 0")) == RollRequest(sides=6, times=1)

    def test_non_numeric_sides_falls_back_to_example(self) -> None:
        assert parse_invocation(_parsed("/roll abc")) is None

    def test_zero_sides_falls_back_to_example(self) -> None:
        assert parse_invocation(_parsed("/roll 0")) is None

    def test_negative_sides_falls_back_to_example(self) -> None:
        assert parse_invocation(_parsed("/roll -5")) is None

    def test_non_numeric_times_falls_back_to_example(self) -> None:
        assert parse_invocation(_parsed("/roll 6 abc")) is None

    def test_absurdly_large_sides_is_accepted(self) -> None:
        """v1 places no upper bound on `limite` (Python ints are unbounded) and
        this port doesn't add one for the `roll` spelling either -- only
        `cb_core/textmatch.py`'s `/d<N>` shorthand caps at 4 digits, which is a
        gap outside this file's ownership (see the module docstring and
        test_five_digit_shorthand_is_a_known_textmatch_gap above)."""
        assert parse_invocation(_parsed("/roll 999999999")) == RollRequest(sides=999999999, times=1)

    def test_extra_arguments_beyond_the_second_are_ignored(self) -> None:
        assert parse_invocation(_parsed("/roll 6 3 hello world")) == RollRequest(sides=6, times=3)


# --------------------------------------------------------------------- render_roll


class TestRenderRoll:
    def test_single_roll_has_no_catalog_line(self) -> None:
        """v1: `if vezes == 1: resposta += f"🎲 -> {random.randint(1, limite)}"`
        (Miscellaneous.py:174-175) — never touches the dice_roll catalog key."""
        assert render_roll(6, [4], "en") == "(d6) \U0001f3b2 -> 4"

    def test_multiple_rolls_use_the_catalog_english(self) -> None:
        text = render_roll(6, [4, 2], "en")
        assert text == "(d6) \n1th Roll: \U0001f3b2 -> 4\n2th Roll: \U0001f3b2 -> 2"

    def test_multiple_rolls_use_the_catalog_portuguese(self) -> None:
        text = render_roll(6, [4, 2], "pt")
        assert text == "(d6) \n1º Lançamento: \U0001f3b2 -> 4\n2º Lançamento: \U0001f3b2 -> 2"

    def test_spanish_falls_back_to_english_catalog_value(self) -> None:
        """es/lib.json has no `dice_roll`/`dice_exemple` keys (unlike v1's own
        es/lib.json, which is equally missing them) -- cb_core.locales.get
        falls back to English, exactly as v1's Localizer.bundle() does by
        deep-merging the default language underneath the target one."""
        assert render_roll(6, [4], "es") == render_roll(6, [4], "en")


# --------------------------------------------------------------------------- roll


class TestRoll:
    def test_result_count_matches_times(self) -> None:
        result = roll(RollRequest(sides=6, times=5))
        assert len(result) == 5

    @pytest.mark.parametrize("sides", [1, 6, 20, 100])
    def test_every_result_within_bounds(self, sides: int) -> None:
        # Not seeded: asserting on the range, not an exact value, per the QA
        # harness rules (no monkeypatching our own randomness).
        for _ in range(200):
            [value] = roll(RollRequest(sides=sides, times=1))
            assert 1 <= value <= sides

    def test_single_sided_die_always_rolls_one(self) -> None:
        assert roll(RollRequest(sides=1, times=10)) == [1] * 10
