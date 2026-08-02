"""Unit coverage for `cb_gateway.handlers.nextbirthday`'s trigger surface.

Pure logic only. End-to-end coverage (gate, the shared text builder, the
`message.answer` vs. `.reply` distinction) is in `qa/test_util_nextbirthday.py`.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from cb_gateway.filters import CommandName


@dataclass
class _FakeMessage:
    text: str | None


@pytest.mark.parametrize(
    "text", ["/nextbirthday", "/nextbirthdays", "/proximosaniversarios", "/proximoscumpleanos"]
)
@pytest.mark.asyncio
async def test_every_v1_alias_resolves(text: str) -> None:
    result = await CommandName("nextbirthday")(_FakeMessage(text), bot_username="CookieMWbot")
    assert result is not False, f"{text!r} did not resolve to the nextbirthday command"


@pytest.mark.asyncio
async def test_addressed_at_a_different_bot_does_not_resolve() -> None:
    result = await CommandName("nextbirthday")(
        _FakeMessage("/nextbirthday@SomeOtherBot"), bot_username="CookieMWbot"
    )
    assert result is False


@pytest.mark.asyncio
async def test_unrelated_command_does_not_resolve() -> None:
    result = await CommandName("nextbirthday")(_FakeMessage("/isalive"), bot_username="CookieMWbot")
    assert result is False
