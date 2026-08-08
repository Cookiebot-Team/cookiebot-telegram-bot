"""Audio fingerprint recognition — the recogniser, and why it is not Shazam's.

v1: `identify_music`, `../COOKIEBOT-Telegram-Group-Bot/Bot/Audio.py:6-20`, which
calls `ShazamAPI.Shazam(content).recognizeSong()` and reads
`response[1]['track']['title']` / `['subtitle']`.

## Why the vendor changed

`scripts/spec.py`'s own row for this feature says "ShazamAPI is unofficial —
feature-flag it behind a breaker", and that is the starting point. What the
port found:

* **`ShazamAPI` is unmaintained**, and wraps an endpoint Shazam does not
  publish. Its maintained successor is `shazamio`, whose fingerprinting is a
  Rust extension (`shazamio-core`).
* **`shazamio-core` cannot be loaded on this workspace's Python.** `import
  shazamio_core` **segfaults** on 3.14 (the workspace requires ≥3.13); its
  `pydub` dependency additionally needs the `audioop` module removed in 3.13.
  A segfault is not something a `try/except ImportError` can contain — it takes
  the worker process down — so an optional extra would not have made it safe
  either. Reproduce with `python -c "import shazamio_core"`.

So this port keeps v1's *behaviour* and changes the vendor, the same trade
`util_postforwarder` already made when it replaced Google Cloud Translate with
the LLM router: same contract, different provider, **no new dependency**. The
recogniser here is [AudD](https://audd.io)'s documented HTTP API, called with
the `httpx` client this codebase already uses for every other outbound call
(AGENTS.md §5) — a published, keyed API rather than a reverse-engineered one.

**Empty key ⇒ the feature is inert**, exactly like `youtube_api_key` and
`saucenao_api_key` already behave, and `CB_MUSIC_DETECTION_ENABLED` defaults to
`false` on top of that. `set_recogniser` remains the seam a deployment can use
to plug in Shazam itself, on a Python where its binding loads.
"""

from __future__ import annotations

from typing import Any, Protocol

import httpx
import msgspec

from cb_core.logging import get_logger
from cb_core.settings import get_settings

log = get_logger("cb.worker.music")

_RECOGNISE_URL = "https://api.audd.io/"


class Track(msgspec.Struct, frozen=True):
    """The two fields v1 reads off the response (`Audio.py:16-17`) and nothing
    else. `title` is the song; `subtitle` is Shazam's word for the artist, and
    the field name is kept so the answer string stays a transcription of v1's.
    """

    title: str
    subtitle: str


class Recogniser(Protocol):
    """What a music-recognition backend has to do. One method, so a deployment
    that has a working Shazam binding can supply it without this module
    knowing anything about it."""

    async def recognise(self, data: bytes, *, timeout: float) -> Track | None: ...


def parse_match(payload: dict[str, Any]) -> Track | None:
    """v1's `if not 'track' in response[1]: return` (`Audio.py:14-17`), against
    AudD's response shape.

    AudD answers `{"status": "success", "result": null}` for "nothing matched"
    and `{"status": "error", ...}` for a rejected request; both are `None`
    here, and the caller reports them differently only through the metric —
    v1 says nothing either way.

    Kept pure so the no-match branch, by far the common one for a voice note
    of someone talking, is tested without a network at all.
    """
    result = payload.get("result")
    if not isinstance(result, dict):
        return None
    title = result.get("title")
    artist = result.get("artist")
    if not isinstance(title, str) or not isinstance(artist, str):
        return None
    return Track(title=title, subtitle=artist)


class AudDRecogniser:
    """The default backend. Nothing but an HTTP POST of the bytes."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient()
        return self._client

    async def recognise(self, data: bytes, *, timeout: float) -> Track | None:
        api_key = get_settings().audd_api_key
        if not api_key:
            log.info("music.no_api_key")
            return None
        response = await self._http().post(
            _RECOGNISE_URL,
            data={"api_token": api_key},
            files={"file": ("voice.ogg", data, "audio/ogg")},
            timeout=httpx.Timeout(timeout),
        )
        response.raise_for_status()
        payload = response.json()
        return parse_match(payload) if isinstance(payload, dict) else None


_recogniser: Recogniser = AudDRecogniser()


def set_recogniser(recogniser: Recogniser | None) -> None:
    """Swap the backend. `None` restores the AudD default.

    The seam tests drive (no network), and the one a deployment uses to plug
    in its own recogniser — see the module docstring.
    """
    global _recogniser
    _recogniser = recogniser if recogniser is not None else AudDRecogniser()


def available() -> bool:
    """Whether a lookup could possibly succeed.

    False with no key configured, so the job answers nothing and never spends
    a request — the same "unset key means the feature is not there" rule
    `util_youtube` and `x_reverse_search` follow. A custom recogniser
    registered through `set_recogniser` is always considered available: only
    it knows what it needs.
    """
    return not isinstance(_recogniser, AudDRecogniser) or bool(get_settings().audd_api_key)


async def recognise(data: bytes, *, timeout: float) -> Track | None:
    """The audio, fingerprinted. `None` means "nothing matched".

    A transport or protocol failure **raises**, so `cb_worker/jobs/music.py`'s
    breaker can count it: "the recogniser is down" and "this is not a song"
    must not look the same to a circuit breaker.
    """
    return await _recogniser.recognise(data, timeout=timeout)


__all__ = [
    "AudDRecogniser",
    "Recogniser",
    "Track",
    "available",
    "parse_match",
    "recognise",
    "set_recogniser",
]
