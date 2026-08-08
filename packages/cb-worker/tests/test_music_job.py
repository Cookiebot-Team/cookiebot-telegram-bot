"""`cb_worker.jobs.music` and `cb_worker.music` — every branch of v1's
`identify_music`, plus the breaker its unofficial endpoint needs.

The backend is swapped through `music.set_recogniser`, so nothing here opens a
socket. The two things that are genuinely v1's — the "no track, say nothing"
branch and the two hardcoded answer strings — are asserted directly, alongside
the breaker that this feature needs and `/youtube` does not.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from cb_worker import music
from cb_worker.jobs import music as job
from cb_worker.music import Track


class _FakeBuffer:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class _FakeBot:
    def __init__(self, *, download: bytes | None = b"ogg-bytes") -> None:
        self._download = download
        self.messages: list[dict[str, Any]] = []

    async def download(self, file_id: str) -> _FakeBuffer:
        if self._download is None:
            raise RuntimeError("no such file")
        return _FakeBuffer(self._download)

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> None:
        self.messages.append({"chat_id": chat_id, "text": text, **kwargs})


class _Recogniser:
    """A backend, without a network. `cb_worker.music.Recogniser`'s one method."""

    def __init__(self, track: Track | None = None, *, error: Exception | None = None) -> None:
        self.track = track
        self.error = error
        self.calls = 0

    async def recognise(self, data: bytes, *, timeout: float) -> Track | None:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.track


HIT = Track("Never Gonna Give You Up", "Rick Astley")

#: AudD's own response shape, which `parse_match` reads (`music.py`).
AUDD_HIT = {
    "status": "success",
    "result": {"title": "Never Gonna Give You Up", "artist": "Rick Astley"},
}


@pytest.fixture(autouse=True)
def _reset() -> Any:
    music.set_recogniser(None)
    job.breaker().record(True, time.monotonic())
    yield
    music.set_recogniser(None)
    job.breaker().record(True, time.monotonic())


async def _run(bot: _FakeBot, lang: str = "en") -> None:
    await job.identify_music(
        {"bot": bot}, group_id=-100, message_id=5, file_id="voice-1", lang=lang
    )


# --------------------------------------------------------------- response shape


def test_a_match_is_parsed_into_title_and_subtitle() -> None:
    assert music.parse_match(AUDD_HIT) == HIT


@pytest.mark.parametrize(
    "payload",
    [
        {},  # v1: `if not 'track' in response[1]: return`
        {"status": "success", "result": None},  # AudD's own "nothing matched"
        {"status": "error", "error": {"error_code": 901}},  # a rejected request
        {"result": {}},  # a result with neither field is not an answer
        {"result": {"title": "Only a title"}},
    ],
)
def test_anything_without_both_fields_is_no_match(payload: dict[str, Any]) -> None:
    assert music.parse_match(payload) is None


# ------------------------------------------------------------------- the answer


def test_english_groups_get_song() -> None:
    assert job.answer_for(Track("T", "S"), "en") == "SONG: 🎵 <b> T </b> - <i> S </i> 🎵"


@pytest.mark.parametrize("lang", ["pt", "es"])
def test_portuguese_and_spanish_share_one_string(lang: str) -> None:
    """v1's `if language in ['pt', 'es']` (`Audio.py:18`) — a Spanish group is
    answered in Portuguese. Preserved, wart included; there is no catalog key
    to translate it with."""
    assert job.answer_for(Track("T", "S"), lang).startswith("MÚSICA:")


# ---------------------------------------------------------------------- the job


async def test_a_match_is_replied_to_the_voice_note() -> None:
    music.set_recogniser(_Recogniser(HIT))
    bot = _FakeBot()
    await _run(bot)
    assert len(bot.messages) == 1
    assert bot.messages[0]["reply_to_message_id"] == 5
    assert bot.messages[0]["parse_mode"] == "HTML"
    assert "Rick Astley" in bot.messages[0]["text"]


async def test_no_match_says_nothing() -> None:
    """The common case — most voice notes are people talking. v1 returns
    silently (`Audio.py:14-15`)."""
    music.set_recogniser(_Recogniser(None))
    bot = _FakeBot()
    await _run(bot)
    assert bot.messages == []


async def test_no_configured_key_says_nothing_and_costs_no_request() -> None:
    """The default deployment: `CB_AUDD_API_KEY` unset. Same "unset key means
    the feature is not there" rule `util_youtube` and `x_reverse_search`
    follow — and a group must never be told about a deployment gap."""
    music.set_recogniser(None)  # back to the real AudD backend, with no key
    assert not music.available()
    bot = _FakeBot()
    await _run(bot)
    assert bot.messages == []


async def test_a_download_failure_says_nothing() -> None:
    music.set_recogniser(_Recogniser(HIT))
    bot = _FakeBot(download=None)
    await _run(bot)
    assert bot.messages == []


# ----------------------------------------------------------------- the breaker


async def test_the_breaker_opens_after_repeated_failures_and_stops_calling() -> None:
    """The reason this feature has a breaker at all and `/youtube` does not:
    it fires on *every* voice note, so a Shazam outage would otherwise mean one
    doomed outbound call per note, forever (FEATURE-MAP §5)."""
    recogniser = _Recogniser(error=RuntimeError("shazam is down"))
    music.set_recogniser(recogniser)
    bot = _FakeBot()

    for _ in range(job.breaker().threshold):
        await _run(bot)
    assert recogniser.calls == job.breaker().threshold
    assert job.breaker().is_open

    await _run(bot)
    assert recogniser.calls == job.breaker().threshold, "the breaker let a call through"
    assert bot.messages == []


async def test_a_success_closes_the_breaker_again() -> None:
    failing = _Recogniser(error=RuntimeError("down"))
    music.set_recogniser(failing)
    bot = _FakeBot()
    for _ in range(job.breaker().threshold - 1):
        await _run(bot)
    assert not job.breaker().is_open

    music.set_recogniser(_Recogniser(HIT))
    await _run(bot)
    assert not job.breaker().is_open
    assert len(bot.messages) == 1
