"""Unit coverage for the core_privacy trigger surface.

Pure logic only (no dispatcher, no Telegram, no DB): every v1 alias must resolve
to the `privacy` handler through `CommandName`, and a command addressed at a
different bot must not. See docs/contracts/core_privacy.md for the full
behaviour contract and the acceptance scenarios in
qa/features/core_privacy.feature for the end-to-end version of the same
assertions.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from cb_gateway.filters import CommandName


@dataclass
class _FakeMessage:
    """CommandName only ever reads `.text` off the message it's given."""

    text: str | None


@pytest.mark.parametrize(
    "text",
    ["/privacy", "/privacidade", "/privacidad"],
)
@pytest.mark.asyncio
async def test_every_v1_alias_resolves(text: str) -> None:
    result = await CommandName("privacy")(_FakeMessage(text), bot_username="CookieMWbot")
    assert result is not False, f"{text!r} did not resolve to the privacy command"


@pytest.mark.asyncio
async def test_addressed_at_this_bot_resolves() -> None:
    result = await CommandName("privacy")(
        _FakeMessage("/privacy@CookieMWbot"), bot_username="CookieMWbot"
    )
    assert result is not False


@pytest.mark.asyncio
async def test_addressed_at_a_different_bot_does_not_resolve() -> None:
    result = await CommandName("privacy")(
        _FakeMessage("/privacy@SomeOtherBot"), bot_username="CookieMWbot"
    )
    assert result is False


@pytest.mark.asyncio
async def test_unrelated_command_does_not_resolve() -> None:
    result = await CommandName("privacy")(_FakeMessage("/isalive"), bot_username="CookieMWbot")
    assert result is False
