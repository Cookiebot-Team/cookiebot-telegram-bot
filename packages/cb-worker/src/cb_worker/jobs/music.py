"""core_musicdetection — a voice note gets identified, if it is a song.

v1: `identify_music`, `../COOKIEBOT-Telegram-Group-Bot/Bot/Audio.py:6-20`,
dispatched from the `voice` content-type branch at `COOKIEBOT.py:155-159`
under the `functionsUtility` gate. Contract:
`docs/contracts/core_musicdetection.md`.

Three things about v1's shape are worth stating, because they explain every
decision here:

* **It is passive.** No command; *every* voice note in a group with utility
  functions on is fingerprinted. That makes it the highest-volume outbound
  call in the whole bot, which is why the breaker below matters more than it
  does for `/youtube` or `/buscarfonte`.
* **It says nothing when nothing matched** (`Audio.py:14-15`), which is the
  overwhelmingly common case — most voice notes are people talking. Silence
  is the ported behaviour, not a gap.
* **The two answer strings are hardcoded in v1**, not catalog keys, with a
  `language in ['pt', 'es']` split that gives Spanish groups the Portuguese
  wording (`Audio.py:18-20`). Reproduced verbatim, wart included — the
  alternative is inventing copy for a live feature.

v1 called Shazam inline on a handler thread with no timeout and no breaker,
which FEATURE-MAP §5 names as one of the four reasons its 50-thread pool
wedged. Here it is a job, bounded by `music_detection_timeout_seconds` and
skipped fast while `cb_core.breaker` is open.

Does not import `cb_worker.main` — `main.py` imports this module to register
it. The telemetry wrapper is copied from `youtube.py`, not imported, for that
reason.
"""

from __future__ import annotations

import time
from typing import Any

from aiogram import Bot
from opentelemetry.trace import SpanKind
from prometheus_client import Counter

from cb_core.breaker import Breaker
from cb_core.logging import get_logger
from cb_core.metrics import job_duration
from cb_core.settings import get_settings
from cb_core.telemetry import context_from_carrier, span
from cb_worker.music import Track, available, recognise

log = get_logger("cb.worker.music")

# outcome in identified|no_match|unavailable|breaker_open|error. Never a group
# or user id (AGENTS.md §7), and never the track title — that is unbounded.
music_detection_total = Counter(
    "cb_worker_music_detection_total", "Voice notes fingerprinted", ["outcome"]
)

#: One shared breaker, same defaults `util_doomlist`'s outbound lookups use.
#: Opens after five consecutive failures and lets one request through every
#: thirty seconds after that.
_breaker = Breaker()


def breaker() -> Breaker:
    """Exposed so tests can drive it, and so a future health endpoint can read
    it — the doomlist's own breakers are reached the same way."""
    return _breaker


def answer_for(track: Track, lang: str) -> str:
    """v1's two hardcoded strings (`Audio.py:18-20`), byte for byte.

    Note the `['pt', 'es']` grouping: a Spanish group is answered in
    Portuguese. That is v1's behaviour, it is what live groups see today, and
    there is no catalog key to translate it with — a v2-invented Spanish
    string would be a copy change, not a port.
    """
    label = "MÚSICA" if lang in ("pt", "es") else "SONG"
    return f"{label}: 🎵 <b> {track.title} </b> - <i> {track.subtitle} </i> 🎵"


async def identify_music(
    ctx: dict[str, Any], *, group_id: int, message_id: int, file_id: str, lang: str
) -> None:
    parent = context_from_carrier(ctx.get("trace_carrier"))
    start = time.perf_counter()
    outcome = "ok"
    token = None
    try:
        from opentelemetry import context as otel_context

        token = otel_context.attach(parent)
        with span("job.identify_music", kind=SpanKind.CONSUMER):
            await _run(ctx["bot"], group_id, message_id, file_id, lang)
    except Exception:
        outcome = "error"
        log.exception("job.failed", job="identify_music")
        raise
    finally:
        if token is not None:
            from opentelemetry import context as otel_context

            otel_context.detach(token)
        job_duration.labels(job="identify_music", outcome=outcome).observe(
            time.perf_counter() - start
        )


async def _run(bot: Bot, group_id: int, message_id: int, file_id: str, lang: str) -> None:
    if not available():
        # The optional extra is not installed. Nothing to say — v1's own
        # no-match branch is silent too, and a group must never be told about
        # a missing deployment dependency.
        music_detection_total.labels(outcome="unavailable").inc()
        return

    now = time.monotonic()
    if not _breaker.allow(now):
        log.info("music.breaker_open")
        music_detection_total.labels(outcome="breaker_open").inc()
        return

    data = await _download(bot, file_id)
    if data is None:
        music_detection_total.labels(outcome="error").inc()
        return

    settings = get_settings()
    try:
        track = await recognise(data, timeout=settings.music_detection_timeout_seconds)
    except Exception as exc:  # noqa: BLE001 - an unofficial endpoint failing is expected
        _breaker.record(False, time.monotonic())
        log.warning("music.recognise_failed", error=str(exc))
        music_detection_total.labels(outcome="error").inc()
        return
    _breaker.record(True, time.monotonic())

    if track is None:
        # v1: `if not 'track' in response[1]: return` — silence (`Audio.py:14`).
        music_detection_total.labels(outcome="no_match").inc()
        return

    await bot.send_message(
        group_id, answer_for(track, lang), parse_mode="HTML", reply_to_message_id=message_id
    )
    music_detection_total.labels(outcome="identified").inc()


async def _download(bot: Bot, file_id: str) -> bytes | None:
    """`reverse_search.py`/`transcribe.py`'s idiom, for the same reason."""
    try:
        buffer = await bot.download(file_id)
    except Exception as exc:  # noqa: BLE001 - Telegram is the outside world
        log.warning("music.download_failed", error=str(exc))
        return None
    return None if buffer is None else buffer.read()


__all__ = ["answer_for", "breaker", "identify_music", "music_detection_total"]
