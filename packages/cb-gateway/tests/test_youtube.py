"""Unit coverage for `cb_gateway.handlers.youtube`'s trigger surface.

Pure logic only (no dispatcher, no Telegram, no DB): every v1 alias must
resolve through `CommandName`. The gate/no-query/enqueue behaviour is covered
end-to-end in `qa/test_util_youtube.py`; see `docs/contracts/util_youtube.md`.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from cb_gateway.filters import CommandName


@dataclass
class _FakeMessage:
    text: str | None


@pytest.mark.asyncio
async def test_youtube_resolves() -> None:
    result = await CommandName("youtube")(_FakeMessage("/youtube cake"), bot_username="CookieMWbot")
    assert result is not False


@pytest.mark.asyncio
async def test_addressed_at_a_different_bot_does_not_resolve() -> None:
    result = await CommandName("youtube")(
        _FakeMessage("/youtube@SomeOtherBot cake"), bot_username="CookieMWbot"
    )
    assert result is False


@pytest.mark.asyncio
async def test_unrelated_command_does_not_resolve() -> None:
    result = await CommandName("youtube")(_FakeMessage("/isalive"), bot_username="CookieMWbot")
    assert result is False
