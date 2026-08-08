"""Unit coverage for `cb_gateway.handlers.destroy`'s pure surface.

The reply-resolution table is the whole of v1's `elif` chain
(`Miscellaneous.py:393-433`) and is what decides between "distort this" and
one of the three refusals, so it is worth asserting branch by branch without
a dispatcher. End-to-end behaviour is `qa/test_x_distortion.py`; the pixels
are `packages/cb-worker/tests/test_distort.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from cb_gateway.filters import CommandName
from cb_gateway.handlers.destroy import (
    REFUSE_GIF,
    REFUSE_INSTRUCTIONS,
    REFUSE_VIDEO,
    resolve_reply,
    wants_own_photo,
)


@dataclass
class _FakeMessage:
    text: str | None


@dataclass
class _Size:
    file_id: str


@dataclass
class _Sticker:
    file_id: str
    is_animated: bool = False
    is_video: bool = False


@dataclass
class _File:
    file_id: str


@dataclass
class _Reply:
    """Only the fields `resolve_reply` reads."""

    video: Any = None
    photo: list[_Size] | None = None
    audio: Any = None
    voice: Any = None
    sticker: Any = None
    animation: Any = None


# ------------------------------------------------------------------- triggers


@pytest.mark.parametrize("alias", ["/destroy", "/zoar", "/destruir"])
@pytest.mark.asyncio
async def test_every_v1_spelling_resolves(alias: str) -> None:
    result = await CommandName("destroy")(_FakeMessage(alias), bot_username="CookieMWbot")
    assert result is not False


@pytest.mark.asyncio
async def test_addressed_at_a_different_bot_does_not_resolve() -> None:
    result = await CommandName("destroy")(
        _FakeMessage("/destroy@SomeOtherBot"), bot_username="CookieMWbot"
    )
    assert result is False


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/destroy pfp", True),
        # v1 tests the whole message, not the argument (`:379`), so this matches too.
        ("/destroy my pfp", True),
        ("/destroy", False),
        ("/destroy pfpx", False),
    ],
)
def test_pfp_branch_matches_v1s_endswith(text: str, expected: bool) -> None:
    assert wants_own_photo(text) is expected


# --------------------------------------------------------- reply resolution


def test_video_is_refused_before_anything_else() -> None:
    """v1 checks `'video' in reply` first (`:395`), so a message carrying both
    a video and a photo is refused rather than distorted."""
    assert resolve_reply(_Reply(video=_File("v"), photo=[_Size("p")])) == (REFUSE_VIDEO, None)


def test_photo_resolves_to_the_largest_size() -> None:
    assert resolve_reply(_Reply(photo=[_Size("small"), _Size("large")])) == ("photo", "large")


def test_audio_and_voice_are_the_same_kind() -> None:
    """v1 sends both through `distort_audiofile` and answers both with
    `sendAudio` (`:405-416`)."""
    assert resolve_reply(_Reply(audio=_File("a"))) == ("audio", "a")
    assert resolve_reply(_Reply(voice=_File("v"))) == ("audio", "v")


def test_static_sticker_is_distorted() -> None:
    assert resolve_reply(_Reply(sticker=_Sticker("s"))) == ("sticker", "s")


@pytest.mark.parametrize("flag", ["is_animated", "is_video"])
def test_moving_stickers_are_refused_as_gifs(flag: str) -> None:
    sticker = _Sticker("s", **{flag: True})
    assert resolve_reply(_Reply(sticker=sticker)) == (REFUSE_GIF, None)


def test_animation_is_refused_as_a_gif() -> None:
    assert resolve_reply(_Reply(animation=_File("g"))) == (REFUSE_GIF, None)


def test_anything_else_gets_the_instructions() -> None:
    assert resolve_reply(_Reply()) == (REFUSE_INSTRUCTIONS, None)
