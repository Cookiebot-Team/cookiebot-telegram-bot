"""`/destroy`'s download → distort → send, off the reply path.

v1: `destroy`, `../COOKIEBOT-Telegram-Group-Bot/Bot/Miscellaneous.py:377-433`,
dispatched `COOKIEBOT.py:242-243` under the `funfunctions` gate. The branch
decisions (which media, which refusal) stay in
`cb_gateway/handlers/destroy.py` — they are free and synchronous, and v1 makes
them in the same order. Everything after them is here.

**Why this is the clearest §2.4 case in the v1 tree.** v1 ran the download,
the seam carve and the ffmpeg pass inline in the handler thread, serialised by
three module-global booleans it *spun* on:

    while SEMAPHORE_IMAGES:
        pass                       # Distortioner.py:145-146, also :114, :155

That is FEATURE-MAP D3: a second concurrent `/destroy` burned a whole core
doing nothing until the first finished. It also wrote every intermediate to a
fixed filename in the process's working directory (`distorted.jpg`,
`distorted.mp3`, `preprocessed.mp4`, `tmp.mp4`) — D4, a cross-request race
that silently returned another user's image. Here the bound is a real
`asyncio.Semaphore`, the CPU work runs in a thread, and every temporary file
lives in a per-call `TemporaryDirectory` (`cb_worker/distort.py`).

Does not import `cb_worker.main` — `main.py` imports this module to register
it. The telemetry wrapper is copied from `youtube.py`/`reverse_search.py`, not
imported, for that reason.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from aiogram import Bot
from aiogram.types import BufferedInputFile
from opentelemetry.trace import SpanKind
from prometheus_client import Counter

from cb_core.locales import get_nested as locale_nested
from cb_core.logging import get_logger
from cb_core.metrics import job_duration
from cb_core.settings import get_settings
from cb_core.telemetry import context_from_carrier, span
from cb_worker.distort import distort_audio_bytes, distort_image_async

log = get_logger("cb.worker.distortion")

#: What the gateway resolved the reply into. `photo` and `sticker` differ only
#: in the output format and the send method — v1 writes `distorted.jpg` and
#: `sendPhoto` for one (`Miscellaneous.py:381,404`), `distorted.png` and
#: `sendSticker` for the other (`:423-425`).
KINDS = ("photo", "sticker", "audio")

# kind x outcome, both bounded; never a group or user id (AGENTS.md §7).
distortion_total = Counter(
    "cb_worker_distortion_total", "Media distorted by /destroy", ["kind", "outcome"]
)

_semaphore: asyncio.Semaphore | None = None


def concurrency_bound() -> asyncio.Semaphore:
    """The real replacement for v1's spin lock, created lazily so the bound is
    read from settings at first use rather than at import."""
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(get_settings().distortion_concurrency)
    return _semaphore


def reset_concurrency_bound() -> None:
    """Test seam: drop the memoised semaphore so a test can set its own bound."""
    global _semaphore
    _semaphore = None


async def distort_media(
    ctx: dict[str, Any], *, group_id: int, message_id: int, file_id: str, kind: str, lang: str
) -> None:
    parent = context_from_carrier(ctx.get("trace_carrier"))
    start = time.perf_counter()
    outcome = "ok"
    token = None
    try:
        from opentelemetry import context as otel_context

        token = otel_context.attach(parent)
        with span("job.distort_media", kind=SpanKind.CONSUMER):
            await _run(ctx["bot"], group_id, message_id, file_id, kind, lang)
    except Exception:
        outcome = "error"
        log.exception("job.failed", job="distort_media")
        raise
    finally:
        if token is not None:
            from opentelemetry import context as otel_context

            otel_context.detach(token)
        job_duration.labels(job="distort_media", outcome=outcome).observe(
            time.perf_counter() - start
        )


async def _run(
    bot: Bot, group_id: int, message_id: int, file_id: str, kind: str, lang: str
) -> None:
    if kind not in KINDS:  # pragma: no cover - the gateway is the only producer
        log.warning("distortion.unknown_kind", kind=kind)
        return

    data = await _download(bot, file_id)
    if data is None:
        await _fail(bot, group_id, message_id, kind, lang)
        return

    async with concurrency_bound():
        try:
            if kind == "audio":
                distorted = await distort_audio_bytes(data)
            else:
                distorted = await distort_image_async(
                    data, fmt="PNG" if kind == "sticker" else "JPEG"
                )
        except Exception as exc:  # noqa: BLE001 - a bad file is not a worker failure
            # v1 prints the exception and leaves the user with no reply at all
            # (`Distortioner.py:132-133,150-151,160-161`). Answering with its
            # own instruction string is the nearest honest existing message —
            # this port invents no new one.
            log.warning("distortion.failed", kind=kind, error=str(exc))
            await _fail(bot, group_id, message_id, kind, lang)
            return

    await _send(bot, group_id, message_id, kind, distorted)
    distortion_total.labels(kind=kind, outcome="sent").inc()


async def _send(bot: Bot, group_id: int, message_id: int, kind: str, data: bytes) -> None:
    """v1's three send calls, unchanged in method and reply target."""
    if kind == "audio":
        await bot.send_audio(
            group_id,
            BufferedInputFile(data, filename="distorted.mp3"),
            reply_to_message_id=message_id,
        )
        return
    if kind == "sticker":
        await bot.send_sticker(
            group_id,
            BufferedInputFile(data, filename="distorted.png"),
            reply_to_message_id=message_id,
        )
        return
    await bot.send_photo(
        group_id,
        BufferedInputFile(data, filename="distorted.jpg"),
        reply_to_message_id=message_id,
    )


async def _download(bot: Bot, file_id: str) -> bytes | None:
    """`reverse_search.py`'s idiom, for the same reason it gives."""
    try:
        buffer = await bot.download(file_id)
    except Exception as exc:  # noqa: BLE001 - Telegram is the outside world
        log.warning("distortion.download_failed", error=str(exc))
        return None
    return None if buffer is None else buffer.read()


async def _fail(bot: Bot, group_id: int, message_id: int, kind: str, lang: str) -> None:
    try:
        await bot.send_message(
            group_id, locale_nested("destroy", "instru", lang), reply_to_message_id=message_id
        )
    except Exception as exc:  # noqa: BLE001 - the job is over either way
        log.warning("distortion.reply_failed", error=str(exc))
    distortion_total.labels(kind=kind, outcome="error").inc()


__all__ = [
    "KINDS",
    "concurrency_bound",
    "distort_media",
    "distortion_total",
    "reset_concurrency_bound",
]
