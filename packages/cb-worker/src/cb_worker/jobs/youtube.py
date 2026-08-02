"""`/youtube`'s search + reply — design R1.2. v1: `youtube_search`,
`../COOKIEBOT-Telegram-Group-Bot/Bot/SocialContent.py:172-189`, dispatched
`COOKIEBOT.py:248-249,260-261` under the `functionsUtility` gate (the
gate itself, and the no-query check, both stay on the reply path —
`cb_gateway/handlers/youtube.py` — since neither touches the network; only
the YouTube call and its reply move here, per AGENTS.md §2.4's "nothing slow
on the reply path," and v1 had none of it bounded at all — `googleapiclient`
carries no timeout, this job's `settings.youtube_timeout_seconds` (default
5s, D-YT-1) is a v2-only addition, not a preserved value).

Deliberately does not import `cb_worker.main`, same reasoning
`everyone.py`/`calladms.py` already give: `main.py` imports this module to
register it, so this module must not import back. The telemetry wrapper
shape (span, `job_duration`, the `job.failed` log) is copied from those two,
not imported, for the same reason.

Calls the YouTube Data API v3 REST endpoint directly over `httpx` — already
the one HTTP client this codebase uses everywhere else (AGENTS.md §5) —
rather than `google-api-python-client`, v1's dependency for this single
`search().list(...)` call.
"""

from __future__ import annotations

import random
import time
from typing import Any

import httpx
from aiogram import Bot
from aiogram.types import ReactionTypeEmoji
from opentelemetry.trace import SpanKind
from prometheus_client import Counter

from cb_core.locales import get as locale_get
from cb_core.logging import get_logger
from cb_core.metrics import job_duration
from cb_core.settings import get_settings
from cb_core.telemetry import context_from_carrier, span

log = get_logger("cb.worker.youtube")

_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"

# outcome in sent|not_found|error — error is a request-level failure (bad key,
# timeout, non-2xx); not_found is a real empty result, same as v1's own
# youtube_no_find branch. Never a group id or the query itself (AGENTS.md §7).
youtube_search_total = Counter(
    "cb_worker_youtube_search_total", "YouTube searches performed by /youtube", ["outcome"]
)

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient()
    return _client


def set_http_client(client: httpx.AsyncClient | None) -> None:
    """Test seam: swap in a client backed by `httpx.MockTransport`
    (`doomlist.py`'s identical pattern) so unit tests can simulate YouTube —
    including "the API key is wrong" or "the request timed out" — without any
    real network access. `None` restores the default client."""
    global _client
    _client = client


async def _search(query: str) -> list[dict[str, Any]] | None:
    """The up-to-10 `items` from a `search.list` call, or `None` on any
    request-level failure — a distinct outcome from "zero real results"
    (empty list), which the caller reports differently (design R2.2)."""
    settings = get_settings()
    if not settings.youtube_api_key:
        log.warning("youtube.no_api_key")
        return None
    try:
        response = await _get_client().get(
            _SEARCH_URL,
            params={
                "part": "snippet",
                "q": query,
                "type": "video",
                "maxResults": 10,
                "key": settings.youtube_api_key,
            },
            timeout=httpx.Timeout(settings.youtube_timeout_seconds),
        )
        response.raise_for_status()
        items = response.json().get("items")
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("youtube.search_failed", error=str(exc))
        return None
    return items if isinstance(items, list) else None


async def search_youtube(
    ctx: dict[str, Any], *, group_id: int, message_id: int, query: str, lang: str
) -> None:
    parent = context_from_carrier(ctx.get("trace_carrier"))
    start = time.perf_counter()
    outcome = "ok"
    token = None
    try:
        from opentelemetry import context as otel_context

        token = otel_context.attach(parent)
        with span("job.youtube_search", kind=SpanKind.CONSUMER):
            await _run(ctx["bot"], group_id, message_id, query, lang)
    except Exception:
        outcome = "error"
        log.exception("job.failed", job="youtube_search")
        raise
    finally:
        if token is not None:
            from opentelemetry import context as otel_context

            otel_context.detach(token)
        job_duration.labels(job="youtube_search", outcome=outcome).observe(
            time.perf_counter() - start
        )


async def _run(bot: Bot, group_id: int, message_id: int, query: str, lang: str) -> None:
    items = await _search(query)
    if not items:
        # v1: react_to_message(msg, '🤷', is_big=False) then youtube_no_find
        # (`:181-184`). The job only has a message_id, not a live Message, so
        # the Bot API call replaces `message.react` directly — best-effort,
        # same as every other reaction in this codebase.
        try:
            await bot.set_message_reaction(
                group_id, message_id, reaction=[ReactionTypeEmoji(emoji="🤷")], is_big=False
            )
        except Exception as exc:  # noqa: BLE001 - a reaction failing is never worth aborting for
            log.warning("youtube.reaction_failed", error=str(exc))
        await bot.send_message(
            group_id, locale_get("youtube_no_find", lang), reply_to_message_id=message_id
        )
        youtube_search_total.labels(outcome="not_found" if items is not None else "error").inc()
        return

    video = random.choice(items)
    video_id = video.get("id", {}).get("videoId", "")
    description = video.get("snippet", {}).get("description", "")
    text = f"<i> https://www.youtube.com/watch?v={video_id} </i>\n\n<b> {description} </b>"
    await bot.send_message(group_id, text, parse_mode="HTML", reply_to_message_id=message_id)
    youtube_search_total.labels(outcome="sent").inc()


__all__ = ["search_youtube", "set_http_client", "youtube_search_total"]
