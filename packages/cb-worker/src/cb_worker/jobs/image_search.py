"""x_image_search's Google call and send loop — v1: `qualquer_coisa`,
`../COOKIEBOT-Telegram-Group-Bot/Bot/SocialContent.py:147-170`, dispatched
`COOKIEBOT.py:283-289`.

The gate, the `//` guard, the quota and the blocklist all stay on the reply
path (`cb_gateway/handlers/image_search.py`) — none of them touches the
network, and v1 checks them first too. What moves here is the search itself
and the up-to-ten attempts at making Telegram fetch a remote URL, which v1 ran
inline for **every unrecognised command in every group** (AGENTS.md §2.4).

Structure copied from `youtube.py` (the same shape of job: one external
search, one reply), including the `set_http_client` seam and the deliberate
lack of an import back into `cb_worker.main`.

## The Custom Search JSON API, not `google_images_search`

v1's dependency (`SocialContent.py:19`) is a thin wrapper over
`https://www.googleapis.com/customsearch/v1`. Its `search({'q': ..., 'num': 10,
'safe': ..., 'filetype': 'jpg|gif|png'})` maps to the query parameters below
one for one — `searchType=image` is what the wrapper always sets, `fileType`
takes the same pipe-joined list, and `safe` takes v1's own two values. Calling
the endpoint over the shared `httpx` client is what `util_youtube` already
does with the YouTube Data API, rather than a second HTTP stack for one call.

`safe` is `'off'` or `'medium'` exactly as v1 chooses them from the group's
`sfw` flag (`:153-156`). Note that Google retired `medium` in favour of
`active`/`off`, and it is sent unchanged anyway: v1's request is the contract,
and an unknown value there falls back to Google's own default rather than
failing the request — changing it would silently change what an SFW group
sees.
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

log = get_logger("cb.worker.image_search")

_SEARCH_URL = "https://www.googleapis.com/customsearch/v1"

#: v1: `'filetype':'jpg|gif|png'` (`SocialContent.py:154,156`).
_FILE_TYPES = "jpg|gif|png"

# outcome in sent|not_found|error. Never the query, never a group id
# (AGENTS.md §7) — the query is user text and would be both unbounded and
# personal.
image_search_total = Counter(
    "cb_worker_image_search_total",
    "Google image searches performed by the /anything catch-all",
    ["outcome"],
)

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient()
    return _client


def set_http_client(client: httpx.AsyncClient | None) -> None:
    """Test seam, same as `youtube.set_http_client`: swap in a client backed by
    `httpx.MockTransport` so a unit test can simulate Google without a
    network."""
    global _client
    _client = client


def is_animation(url: str) -> bool:
    """v1: `if 'gif' in image.url` (`SocialContent.py:161`) — a substring test
    against the whole URL, not an extension check, so
    `https://example.com/gifts/cat.png` is sent as an animation.

    Preserved: a photo sent through `sendAnimation` is delivered by Telegram
    either way, so the wart costs nothing, and a "corrected" version would
    change which API call a given result goes through for no visible gain.
    """
    return "gif" in url.lower()


async def _search(query: str, *, safe: str) -> list[dict[str, Any]] | None:
    """Up to ten image results, or `None` on any request-level failure — the
    same three-way outcome `youtube._search` returns, for the same reason: an
    empty list is "Google found nothing", `None` is "we could not ask"."""
    settings = get_settings()
    if not settings.google_search_api_key or not settings.google_search_cx:
        log.warning("image_search.no_credentials")
        return None
    try:
        response = await _get_client().get(
            _SEARCH_URL,
            params={
                "q": query,
                "num": 10,
                "safe": safe,
                "fileType": _FILE_TYPES,
                "searchType": "image",
                "cx": settings.google_search_cx,
                "key": settings.google_search_api_key,
            },
            timeout=httpx.Timeout(settings.google_search_timeout_seconds),
        )
        response.raise_for_status()
        items = response.json().get("items")
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("image_search.search_failed", error=str(exc))
        return None
    return items if isinstance(items, list) else []


async def image_search(
    ctx: dict[str, Any],
    *,
    group_id: int,
    message_id: int,
    query: str,
    safe: str,
    lang: str,
) -> None:
    parent = context_from_carrier(ctx.get("trace_carrier"))
    start = time.perf_counter()
    outcome = "ok"
    token = None
    try:
        from opentelemetry import context as otel_context

        token = otel_context.attach(parent)
        with span("job.image_search", kind=SpanKind.CONSUMER):
            await _run(ctx["bot"], group_id, message_id, query, safe, lang)
    except Exception:
        outcome = "error"
        log.exception("job.failed", job="image_search")
        raise
    finally:
        if token is not None:
            from opentelemetry import context as otel_context

            otel_context.detach(token)
        job_duration.labels(job="image_search", outcome=outcome).observe(
            time.perf_counter() - start
        )


async def _run(bot: Bot, group_id: int, message_id: int, query: str, safe: str, lang: str) -> None:
    await bot.send_chat_action(group_id, "upload_photo")
    items = await _search(query, safe=safe)

    if items:
        # v1 shuffles the ten results and sends the first one Telegram accepts
        # (`:158-166`), because a result URL is a third-party page's image and
        # any of them may 404, block hotlinking or exceed Telegram's fetch
        # limits. The caption is the *referrer* — the page the image came from,
        # not the image URL — which is v1's only credit mechanism here.
        results = list(items)
        random.shuffle(results)
        for item in results:
            url = str(item.get("link", ""))
            if not url:
                continue
            referrer = str(item.get("image", {}).get("contextLink", ""))
            try:
                if is_animation(url):
                    await bot.send_animation(
                        group_id, url, caption=referrer, reply_to_message_id=message_id
                    )
                else:
                    await bot.send_photo(
                        group_id, url, caption=referrer, reply_to_message_id=message_id
                    )
            except Exception as exc:  # noqa: BLE001 - v1's bare except, one per result
                log.info("image_search.send_failed", error=str(exc))
                continue
            image_search_total.labels(outcome="sent").inc()
            return

    # v1: react 🤷 then `anything_no_find` (`:167-170`). Reached both when
    # Google returned nothing and when every result failed to send — v1 draws
    # no distinction, and neither does the user-visible reply; the metric does.
    try:
        await bot.set_message_reaction(
            group_id, message_id, reaction=[ReactionTypeEmoji(emoji="🤷")], is_big=False
        )
    except Exception as exc:  # noqa: BLE001 - a reaction failing is never worth aborting for
        log.warning("image_search.reaction_failed", error=str(exc))
    await bot.send_message(
        group_id, locale_get("anything_no_find", lang), reply_to_message_id=message_id
    )
    image_search_total.labels(outcome="not_found" if items is not None else "error").inc()


__all__ = ["image_search", "image_search_total", "is_animation", "set_http_client"]
