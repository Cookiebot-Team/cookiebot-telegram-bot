"""Unit coverage for `cb_gateway.handlers.musicdetection`.

One thing matters here and it is invisible in production when it is wrong:
the handler must raise `SkipHandler` on **every** path, including the one
where it enqueued. v1 runs the music check and the transcribe→AI sub-step
from the same `voice` branch (`COOKIEBOT.py:156-162`); consuming the update
here would silently disable `x_speech_to_text`'s shape (a) with no error
anywhere. The end-to-end proof is in `qa/test_core_musicdetection.py`; this
asserts the mechanism directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from aiogram.dispatcher.event.bases import SkipHandler

from cb_gateway.handlers import musicdetection


@dataclass
class _Voice:
    file_id: str = "voice-1"


@dataclass
class _Chat:
    id: int = -100
    type: str = "supergroup"


@dataclass
class _Message:
    voice: _Voice | None = field(default_factory=_Voice)
    message_id: int = 7
    chat: _Chat = field(default_factory=_Chat)


class _Ctx:
    group_id = -100
    lang = "en"

    def __init__(self, *, utility: bool) -> None:
        self._utility = utility

    def enabled(self, area: str) -> bool:
        return self._utility


@pytest.fixture
def enqueued(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def _enqueue(job: str, *args: object, **kwargs: object) -> bool:
        calls.append({"job": job, **kwargs})
        return True

    monkeypatch.setattr(musicdetection, "enqueue", _enqueue)
    return calls


def _settings(monkeypatch: pytest.MonkeyPatch, *, enabled: bool) -> None:
    from cb_core.settings import get_settings

    patched = get_settings().model_copy(update={"music_detection_enabled": enabled})
    monkeypatch.setattr(musicdetection, "get_settings", lambda: patched)


def _context(monkeypatch: pytest.MonkeyPatch, *, utility: bool) -> None:
    async def _context_for(bot: Any, message: Any) -> _Ctx:
        return _Ctx(utility=utility)

    monkeypatch.setattr(musicdetection, "context_for", _context_for)


async def test_enqueues_and_still_yields(
    monkeypatch: pytest.MonkeyPatch, enqueued: list[dict[str, Any]]
) -> None:
    _settings(monkeypatch, enabled=True)
    _context(monkeypatch, utility=True)
    with pytest.raises(SkipHandler):
        await musicdetection.identify_music(_Message(), None)  # type: ignore[arg-type]
    assert enqueued and enqueued[0]["file_id"] == "voice-1"


async def test_the_flag_being_off_yields_without_enqueueing(
    monkeypatch: pytest.MonkeyPatch, enqueued: list[dict[str, Any]]
) -> None:
    _settings(monkeypatch, enabled=False)
    with pytest.raises(SkipHandler):
        await musicdetection.identify_music(_Message(), None)  # type: ignore[arg-type]
    assert enqueued == []


async def test_the_utility_gate_being_off_yields_without_enqueueing(
    monkeypatch: pytest.MonkeyPatch, enqueued: list[dict[str, Any]]
) -> None:
    _settings(monkeypatch, enabled=True)
    _context(monkeypatch, utility=False)
    with pytest.raises(SkipHandler):
        await musicdetection.identify_music(_Message(), None)  # type: ignore[arg-type]
    assert enqueued == []
