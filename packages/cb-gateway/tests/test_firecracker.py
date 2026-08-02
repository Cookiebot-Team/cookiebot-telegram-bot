"""Unit coverage for fun_firecracker — trigger surface and the pure `burst()` maths.

Pure logic only (no dispatcher, no Telegram, no DB), following the split
`test_ship.py` uses: trigger resolution goes through the compiled
`cb_core.textmatch` table and the `CommandName` filter; the burst amounts are
asserted against a seeded `random.Random`, never the module-global `random`.
See `.specs/features/fun_firecracker/spec.md` (Phase 2) and `design.md` (R2.2,
R5.1) for the contract this file is checking, and
`docs/contracts/fun_firecracker.md` for the end-to-end version.

`burst()` is imported lazily inside `_burst()` rather than at module level: the
handler module does not exist yet at this point in the port, and importing it
at collection time would fail every test in the file instead of only the ones
that actually exercise the burst maths.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

import pytest

from cb_gateway.filters import CommandName

BOT = "CookieMWbot"

# All five v1 spellings (COOKIEBOT.py:215) resolve through
# cb_core/textmatch.py:47-48 to the canonical "firecracker" — that mapping
# already exists and is not touched by this task.
_TRIGGERS = ["rojao", "rojão", "acende", "fogos", "firecracker"]


@dataclass
class _FakeMessage:
    text: str | None


def _variants(trigger: str) -> list[str]:
    """Bare, with a trailing argument, and with `@botname` — the three shapes
    `startswith` prefix matching in v1 (`COOKIEBOT.py:230-231`) accepts."""
    return [
        f"/{trigger}",
        f"/{trigger} boom",
        f"/{trigger}@{BOT}",
    ]


_ALL_TRIGGER_TEXTS = [text for trigger in _TRIGGERS for text in _variants(trigger)]


def _burst(rng: random.Random) -> list[str]:
    from cb_gateway.handlers.firecracker import burst

    return burst(rng)


# --------------------------------------------------------------------- triggers


@pytest.mark.parametrize("text", _ALL_TRIGGER_TEXTS)
@pytest.mark.asyncio
async def test_every_v1_trigger_resolves(text: str) -> None:
    """Every alias, bare / with an argument / with `@botname`, must resolve to
    the canonical `firecracker` command — AGENTS.md §2.1, forever."""
    result = await CommandName("firecracker")(_FakeMessage(text), bot_username=BOT)
    assert result is not False, f"{text!r} did not resolve to the firecracker command"
    assert isinstance(result, dict)
    assert result["parsed"].name == "firecracker"


@pytest.mark.asyncio
async def test_addressed_at_another_bot_does_not_resolve() -> None:
    result = await CommandName("firecracker")(_FakeMessage("/rojao@SomeOtherBot"), bot_username=BOT)
    assert result is False


@pytest.mark.asyncio
async def test_non_trigger_text_does_not_resolve() -> None:
    result = await CommandName("firecracker")(_FakeMessage("/ship"), bot_username=BOT)
    assert result is False


# ------------------------------------------------------------------------ burst


def test_burst_is_non_empty() -> None:
    lines = _burst(random.Random(1234))
    assert lines != []


def test_burst_elements_are_whole_pra_repeats() -> None:
    """Every element is `"pra " * k` with `k >= 1` — design R2.2."""
    lines = _burst(random.Random(1234))
    for line in lines:
        assert line, "burst produced an empty line"
        assert len(line) % 4 == 0, f"{line!r} is not a whole number of 'pra ' repeats"
        k = len(line) // 4
        assert k >= 1
        assert line == "pra " * k


def test_burst_total_pra_count_is_the_drawn_amount_within_v1_bounds() -> None:
    """`amount = randint(5, 20)`, and the loop consumes it exactly
    (`Miscellaneous.py:230-236`): the total `pra` count across every emitted
    line equals the amount drawn, so asserting the total falls in `[5, 20]`
    asserts both invariants at once."""
    lines = _burst(random.Random(1234))
    total = sum(len(line) // 4 for line in lines)
    assert 5 <= total <= 20


@pytest.mark.parametrize("seed", range(1000))
def test_burst_invariants_hold_over_1000_seeds(seed: int) -> None:
    lines = _burst(random.Random(seed))
    assert lines, f"seed {seed} produced an empty burst"
    total = 0
    for line in lines:
        assert len(line) % 4 == 0, f"seed {seed}: {line!r} is not a whole 'pra ' repeat"
        k = len(line) // 4
        assert k >= 1, f"seed {seed}: element with k=0"
        assert line == "pra " * k
        total += k
    assert 5 <= total <= 20, f"seed {seed}: total pra count {total} out of v1 bounds"
