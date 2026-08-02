"""Unit coverage for `cb_gateway.handlers.birthday`'s trigger surface.

Pure logic only (no dispatcher, no Telegram, no DB): every v1 alias must
resolve. The gate/bare-argument/enqueue behaviour is covered end-to-end in
`qa/test_util_birthday.py`; see `docs/contracts/util_birthday.md`.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from cb_gateway.filters import CommandName


@dataclass
class _FakeMessage:
    text: str | None


@pytest.mark.parametrize(
    "text", ["/birthday", "/aniversario", "/aniversário", "/cumpleanos", "/cumpleaños"]
)
@pytest.mark.asyncio
async def test_every_v1_alias_resolves(text: str) -> None:
    result = await CommandName("birthday")(_FakeMessage(text), bot_username="CookieMWbot")
    assert result is not False, f"{text!r} did not resolve to the birthday command"


@pytest.mark.asyncio
async def test_addressed_at_a_different_bot_does_not_resolve() -> None:
    result = await CommandName("birthday")(
        _FakeMessage("/birthday@SomeOtherBot"), bot_username="CookieMWbot"
    )
    assert result is False


@pytest.mark.asyncio
async def test_unrelated_command_does_not_resolve() -> None:
    result = await CommandName("birthday")(_FakeMessage("/isalive"), bot_username="CookieMWbot")
    assert result is False
