"""Unit coverage for `cb_gateway.handlers.deletereposts`'s trigger surface.

The delete itself is covered against a real database in
`qa/integration/test_scheduled_posts.py`, and the command end to end in
`qa/test_util_deletereposts.py`. Contract:
`docs/contracts/util_deletereposts.md`.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from cb_core.textmatch import COMMAND_ALIASES
from cb_gateway.filters import CommandName


@dataclass
class _FakeMessage:
    text: str | None


@pytest.mark.parametrize(
    "spelling",
    [
        "deleteposts",  # v1 `COOKIEBOT.py:209`
        "apagarposts",  # v1, listed twice in the same tuple
        "deletereposts",  # QA's spelling; v1 never ships it (feature-map.mdx:50)
    ],
)
@pytest.mark.asyncio
async def test_every_spelling_resolves(spelling: str) -> None:
    result = await CommandName("deletereposts")(
        _FakeMessage(f"/{spelling}"), bot_username="CookieMWbot"
    )
    assert result is not False


def test_all_three_spellings_share_one_canonical_name() -> None:
    assert {
        COMMAND_ALIASES["deleteposts"],
        COMMAND_ALIASES["apagarposts"],
        COMMAND_ALIASES["deletereposts"],
    } == {"deletereposts"}


@pytest.mark.asyncio
async def test_an_unrelated_command_does_not_resolve() -> None:
    result = await CommandName("deletereposts")(
        _FakeMessage("/deleteme"), bot_username="CookieMWbot"
    )
    assert result is False
