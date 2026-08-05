"""`/buscarfonte`'s SauceNAO lookup — design R2-R5.

v1: `reverse_search`, `../COOKIEBOT-Telegram-Group-Bot/Bot/SocialContent.py:113-142`,
dispatched `COOKIEBOT.py:212-213` under the `functionsUtility` gate.

**Why this is a job and not a handler, beyond the usual reason.** The usual
reason applies — v1 makes an unbounded external call on the reply path, with no
timeout at either the call site or inside `saucenao_api` (AGENTS.md §2.4). But
the decisive one is the credential leak it also fixes.

v1 does not upload the image. It builds a Telegram file URL —

    image_url = f'https://api.telegram.org/file/bot{cookiebotTOKEN}/{path}'
    #   SocialContent.py:89, via fetch_temp_jpg(only_return_url=True)

— and hands *that* to SauceNAO (`:119-120`), which then fetches it. The bot
token travels to a third party in a query path, lands in its access logs, and
goes wherever that service forwards a referer. Anyone holding it controls the
bot. Spec D-RS-1.

This module never constructs that URL. It downloads the bytes through
`bot.download()` and posts them as a multipart file part, which SauceNAO
accepts as an alternative to `url=`. Downloading here rather than in the
gateway also keeps the arq payload scalar: the file id crosses the queue, the
image does not.

Does not import `cb_worker.main` — `main.py` imports this module to register
it. The telemetry wrapper is copied from `youtube.py`, not imported, for that
reason.
"""

from __future__ import annotations

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

log = get_logger("cb.worker.reverse_search")

_SEARCH_URL = "https://saucenao.com/search.php"

#: v1: `results[0].similarity > 80` — strictly greater, and only ever the first
#: result, even when a later one would clear the bar (`SocialContent.py:129`).
SIMILARITY_THRESHOLD = 80.0

# outcome in found|not_found|rate_limited|error. No group id and no file id
# (AGENTS.md §7).
reverse_search_total = Counter(
    "cb_worker_reverse_search_total",
    "Reverse image searches performed by /buscarfonte",
    ["outcome"],
)

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient()
    return _client


def set_http_client(client: httpx.AsyncClient | None) -> None:
    """Test seam — `youtube.py`/`doomlist.py`'s identical pattern."""
    global _client
    _client = client


class _Outcome:
    """What the search concluded, before it becomes a message."""

    __slots__ = ("author", "kind", "title", "url")

    def __init__(
        self,
        kind: str,
        *,
        title: str = "",
        author: str = "",
        url: str = "",
    ) -> None:
        self.kind = kind  # found | not_found | short_limit | long_limit
        self.title = title
        self.author = author
        self.url = url


def _author_of(data: dict[str, Any]) -> str:
    """SauceNAO names the author differently per index; `saucenao_api`
    normalises across them and this has to as well, or an author v1 displayed
    would silently disappear."""
    for key in ("author_name", "member_name", "creator", "author", "artist"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
        # Some indexes return `creator` as a list of collaborators.
        if isinstance(value, list) and value and isinstance(value[0], str):
            return value[0]
    return ""


def _interpret(payload: dict[str, Any]) -> _Outcome:
    """The response, as v1's three branches see it.

    `saucenao_api` raises `ShortLimitReachedError`/`LongLimitReachedError` off
    exactly these two header fields, and v1 catches them in this order
    (`:121-128`) — so the order here is v1's, not an arbitrary one.
    """
    header = payload.get("header") or {}
    if _negative(header.get("short_remaining")):
        return _Outcome("short_limit")
    if _negative(header.get("long_remaining")):
        return _Outcome("long_limit")

    results = payload.get("results") or []
    if not results:
        return _Outcome("not_found")
    first = results[0]
    data = first.get("data") or {}
    urls = data.get("ext_urls") or []
    try:
        similarity = float((first.get("header") or {}).get("similarity", 0))
    except (TypeError, ValueError):
        return _Outcome("not_found")

    # v1: `results and results[0].urls and results[0].similarity > 80`.
    if not urls or similarity <= SIMILARITY_THRESHOLD:
        return _Outcome("not_found")
    return _Outcome(
        "found",
        title=str(data.get("title") or ""),
        author=_author_of(data),
        url=str(urls[0]),
    )


def _negative(value: object) -> bool:
    """SauceNAO returns these as JSON numbers, but a string has been seen in
    the wild for some indexes, so both are accepted and anything else is
    "not limited" rather than an error."""
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return False
    try:
        return int(value) < 0
    except ValueError:
        return False


def build_answer(outcome: _Outcome, lang: str) -> str:
    """v1's exact assembly (`:131-136`), trailing newlines included."""
    answer = locale_get("reverse_best", lang)
    answer += f'"{outcome.title}"'
    if outcome.author:
        answer += f" - {outcome.author}"
    answer += f"\n{outcome.url}\n\n"
    return answer


async def _search(image: bytes) -> _Outcome:
    """POST the bytes. Every request-level failure is `not_found` (D-RS-3).

    v1 lets anything that is not one of the two rate-limit exceptions propagate
    into the global traceback handler, so a SauceNAO outage is silence in the
    group. Degrading to the nearest existing honest string is the policy
    `util_youtube` and `util_calladms` already set; there is no v1 string for
    "the search itself is broken" and this port does not invent one.
    """
    settings = get_settings()
    if not settings.saucenao_api_key:
        log.warning("reverse_search.no_api_key")
        return _Outcome("not_found")
    try:
        response = await _get_client().post(
            _SEARCH_URL,
            data={
                "api_key": settings.saucenao_api_key,
                "output_type": 2,  # JSON
                "db": 999,  # every index
                "numres": 1,  # v1 only ever reads results[0] anyway
            },
            # The bytes, not a URL. This is D-RS-1's fix — see the module
            # docstring. Changing this back to `url=` re-leaks the bot token.
            files={"file": ("image.jpg", image, "image/jpeg")},
            timeout=httpx.Timeout(settings.saucenao_timeout_seconds),
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("reverse_search.request_failed", error=str(exc))
        return _Outcome("not_found")
    if not isinstance(payload, dict):
        log.warning("reverse_search.malformed_response")
        return _Outcome("not_found")
    return _interpret(payload)


async def search_source(
    ctx: dict[str, Any], *, group_id: int, message_id: int, file_id: str, lang: str
) -> None:
    parent = context_from_carrier(ctx.get("trace_carrier"))
    start = time.perf_counter()
    outcome = "ok"
    token = None
    try:
        from opentelemetry import context as otel_context

        token = otel_context.attach(parent)
        with span("job.reverse_search", kind=SpanKind.CONSUMER):
            await _run(ctx["bot"], group_id, message_id, file_id, lang)
    except Exception:
        outcome = "error"
        log.exception("job.failed", job="reverse_search")
        raise
    finally:
        if token is not None:
            from opentelemetry import context as otel_context

            otel_context.detach(token)
        job_duration.labels(job="reverse_search", outcome=outcome).observe(
            time.perf_counter() - start
        )


async def _run(bot: Bot, group_id: int, message_id: int, file_id: str, lang: str) -> None:
    image = await _download(bot, file_id)
    if image is None:
        # v1 has no branch for this: `fetch_temp_jpg` would raise and the update
        # would die silently. The nearest honest existing string is the same one
        # every other dead end here uses.
        await _reply(bot, group_id, message_id, locale_get("reverse_no_found", lang))
        reverse_search_total.labels(outcome="error").inc()
        return

    result = await _search(image)

    if result.kind == "short_limit":
        await _reply(bot, group_id, message_id, locale_get("reverse_other", lang))
        reverse_search_total.labels(outcome="rate_limited").inc()
        return
    if result.kind == "long_limit":
        await _reply(bot, group_id, message_id, locale_get("reverse_limit", lang))
        reverse_search_total.labels(outcome="rate_limited").inc()
        return

    if result.kind == "found":
        await _react(bot, group_id, message_id, "🫡")
        await _reply(bot, group_id, message_id, build_answer(result, lang))
        reverse_search_total.labels(outcome="found").inc()
        return

    await _react(bot, group_id, message_id, "🤷")
    await _reply(bot, group_id, message_id, locale_get("reverse_no_found", lang))
    reverse_search_total.labels(outcome="not_found").inc()


async def _download(bot: Bot, file_id: str) -> bytes | None:
    """`transcribe.py:73-86`'s idiom, for the same reason it gives."""
    try:
        buffer = await bot.download(file_id)
    except Exception as exc:  # noqa: BLE001 - Telegram is the outside world
        log.warning("reverse_search.download_failed", error=str(exc))
        return None
    if buffer is None:
        return None
    return buffer.read()


async def _react(bot: Bot, group_id: int, message_id: int, emoji: str) -> None:
    """v1 reacts with `is_big=False` on both outcomes (`:135,140`)."""
    try:
        await bot.set_message_reaction(
            group_id, message_id, reaction=[ReactionTypeEmoji(emoji=emoji)], is_big=False
        )
    except Exception as exc:  # noqa: BLE001 - a reaction is never worth aborting for
        log.warning("reverse_search.reaction_failed", error=str(exc))


async def _reply(bot: Bot, group_id: int, message_id: int, text: str) -> None:
    """v1 replies to the command in every branch (`msg_to_reply=msg`).

    Sent without `parse_mode`: the answer interpolates a SauceNAO title and
    author verbatim, and one containing `<` or `&` would be rejected as bad
    HTML — v1's own `send_message` defaults to `parse_mode='HTML'` and would
    lose exactly those replies.
    """
    try:
        await bot.send_message(group_id, text, reply_to_message_id=message_id)
    except Exception as exc:  # noqa: BLE001 - the job's work is done; do not retry it
        log.warning("reverse_search.reply_failed", error=str(exc))


__all__ = [
    "SIMILARITY_THRESHOLD",
    "build_answer",
    "reverse_search_total",
    "search_source",
    "set_http_client",
]
