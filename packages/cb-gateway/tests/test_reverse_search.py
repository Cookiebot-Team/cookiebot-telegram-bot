"""Unit coverage for `cb_gateway.handlers.reverse_search`'s pure surface.

Triggers and the file-id resolution. The gate, the refusals and the enqueue are
covered end to end in `qa/test_x_reverse_search.py`; see
`docs/contracts/x_reverse_search.md`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from cb_core.textmatch import COMMAND_ALIASES
from cb_gateway.filters import CommandName
from cb_gateway.handlers import reverse_search


@dataclass
class _FakeMessage:
    text: str | None


@pytest.mark.parametrize("spelling", ["buscarfonte", "searchsource", "buscarfuente"])
@pytest.mark.asyncio
async def test_every_v1_spelling_resolves(spelling: str) -> None:
    """v1 dispatches all three (`COOKIEBOT.py:212`). None were in
    `COMMAND_ALIASES` before this port — AGENTS.md §2.1."""
    result = await CommandName("searchsource")(
        _FakeMessage(f"/{spelling}"), bot_username="CookieMWbot"
    )
    assert result is not False


def test_all_three_share_one_canonical_name() -> None:
    assert {
        COMMAND_ALIASES["buscarfonte"],
        COMMAND_ALIASES["searchsource"],
        COMMAND_ALIASES["buscarfuente"],
    } == {"searchsource"}


@dataclass
class _Sized:
    file_id: str


@dataclass
class _Replied:
    photo: list[_Sized] = field(default_factory=list)
    document: Any = None


def test_the_largest_photo_size_is_chosen() -> None:
    """v1's `msg['photo'][-1]['file_id']` (`SocialContent.py:88`)."""
    replied = _Replied(photo=[_Sized("small"), _Sized("large")])
    assert reverse_search.file_id_of(replied) == "large"  # type: ignore[arg-type]


def test_a_document_is_used_when_there_is_no_photo() -> None:
    """v1 reaches this through `except KeyError` (`:93-95`)."""
    replied = _Replied(document=_Sized("doc-1"))
    assert reverse_search.file_id_of(replied) == "doc-1"  # type: ignore[arg-type]


def test_a_photo_wins_over_a_document() -> None:
    replied = _Replied(photo=[_Sized("pic")], document=_Sized("doc"))
    assert reverse_search.file_id_of(replied) == "pic"  # type: ignore[arg-type]


def test_a_reply_with_neither_returns_none() -> None:
    """D-RS-5. v1 raises a second `KeyError` on `msg['document']` here and the
    update dies with no reply at all; the caller answers `reverse_image`."""
    assert reverse_search.file_id_of(_Replied()) is None  # type: ignore[arg-type]
