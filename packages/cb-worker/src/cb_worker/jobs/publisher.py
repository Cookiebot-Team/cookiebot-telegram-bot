"""The publisher's two slow halves: render-and-fan-out, and delivery.

v1 ran both on the reply path or on a `threading.Timer`:

  * `prepare_post` + `schedule_post`
    (`../COOKIEBOT-Telegram-Group-Bot/Bot/Publisher.py:182-286`) ran inside the
    callback handler for the approve button — two Google Translate calls, one
    exchange-rate request per priced paragraph, two media uploads, and then one
    `getChat` plus one row write for *every group the bot is in*. AGENTS.md §2.4
    in every clause at once.
  * `scheduler_pull` (`:329-357`) ran from a recursive `threading.Timer(300, …)`
    started in the primary bot process only (`COOKIEBOT.py:448-455`). A crash
    between ticks silently stopped every scheduled post, forever, with nothing
    logged (D-PF-11).

Deliberately does not import `cb_worker.main`, the same reasoning
`everyone.py`/`calladms.py`/`youtube.py` already give: `main.py` imports this
module to register the functions, so this module must not import back. The
telemetry wrapper is copied from those, not imported, for that reason.
"""

from __future__ import annotations

import random
import time
from datetime import datetime
from typing import Any

import httpx
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from opentelemetry.trace import SpanKind
from prometheus_client import Counter
from whenever import Instant

from cb_core import db, members, pending_posts, publisher, scheduled_posts
from cb_core.llm import Message, router
from cb_core.logging import get_logger
from cb_core.metrics import job_duration
from cb_core.settings import get_settings
from cb_core.telemetry import context_from_carrier, span

log = get_logger("cb.worker.publisher")

_RATE_URL = "https://v6.exchangerate-api.com/v6/{key}/latest/{code}"

# outcome in scheduled|skipped|error, counted once per target group.
publisher_fanout_total = Counter(
    "cb_worker_publisher_fanout_total",
    "Per-group outcomes of fanning an approved post out to the network",
    ["outcome"],
)

# outcome in sent|opted_out|dropped|error. `dropped` is a row deleted because
# the target is gone (kicked, chat missing); `error` is a transient failure that
# will be retried on the next tick. Never a group id (AGENTS.md §7).
publisher_delivery_total = Counter(
    "cb_worker_publisher_delivery_total",
    "Per-row outcomes of the scheduled-post delivery sweep",
    ["outcome"],
)

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient()
    return _client


def set_http_client(client: httpx.AsyncClient | None) -> None:
    """Test seam, same shape as `youtube.py`/`doomlist.py`: swap in a client
    backed by `httpx.MockTransport` so the exchange-rate call can be simulated,
    including its failure, with no network. `None` restores the default."""
    global _client
    _client = client


# ------------------------------------------------------------------ external calls


class _RateLookup:
    """One job's worth of exchange rates, fetched at most once per pair.

    v1 issued a fresh request per priced *paragraph* (`Publisher.py:166-168`),
    so an ad with eight priced lines made eight identical calls. The rate does
    not change inside one render.

    `convert_prices_in_text` is synchronous — it is pure logic in `cb_core` and
    v1's is synchronous too — so rates are prefetched by `warm()` before it runs
    and the lookup itself only reads the dict.
    """

    def __init__(self) -> None:
        self._rates: dict[tuple[str, str], float | None] = {}

    async def warm(self, code_from: str, code_target: str) -> None:
        pair = (code_from, code_target)
        if pair in self._rates:
            return
        self._rates[pair] = await self._fetch(code_from, code_target)

    def __call__(self, code_from: str, code_target: str) -> float | None:
        return self._rates.get((code_from, code_target))

    async def _fetch(self, code_from: str, code_target: str) -> float | None:
        settings = get_settings()
        if not settings.exchangerate_api_key:
            return None
        try:
            response = await _get_client().get(
                _RATE_URL.format(key=settings.exchangerate_api_key, code=code_from),
                timeout=httpx.Timeout(settings.exchangerate_timeout_seconds),
            )
            response.raise_for_status()
            rate = response.json()["conversion_rates"][code_target]
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            # v1 catches this per paragraph and emits the paragraph unchanged
            # (`:171-172`); returning None reaches the same branch.
            log.warning("publisher.rate_failed", pair=f"{code_from}->{code_target}", error=str(exc))
            return None
        return float(rate)


async def _translate(text: str, target: str, *, group_id: int) -> str:
    """v1's `translate(text, dest)` (`universal_funcs.py:139-161`), on the router.

    Any failure returns the input unchanged. v1 reaches the same outcome by a
    stranger route: its Google client returns an HTML error page rather than
    raising, so `prepare_post` sniffs the string `'Error 500 (Server Error)'`
    and discards the translation (`:206-209`). The check is not ported; its
    effect is (design R5.2).
    """
    if not text.strip():
        return text
    try:
        completion = await router().complete(
            "translate",
            [Message(role="user", content=text)],
            group_id=group_id,
            system=(
                f"Translate the user's message into {target}. Reply with the "
                "translation only — no preamble, no quotes, no explanation. "
                "Preserve line breaks, URLs, emoji and any HTML tags exactly."
            ),
        )
    except Exception as exc:  # noqa: BLE001 - a failed translation is a caption, not an outage
        log.warning("publisher.translate_failed", target=target, error=str(exc))
        return text
    return completion.text or text


# ---------------------------------------------------------------------- the render


async def _render(
    bot: Bot,
    post: pending_posts.PendingPost,
    *,
    origin_title: str,
    origin_username: str | None,
    author_first_name: str | None,
    author_username: str | None,
    group_id: int,
) -> tuple[int, int]:
    """`prepare_post` (`:182-221`) — returns the pt and en message ids.

    The keyboard is built once and shared by both sends, as in v1: it is
    derived from the caption's URLs, which the translation does not change.
    """
    settings = get_settings()
    buttons, caption = publisher.build_post_keyboard(
        caption=post.caption,
        caption_entity_urls=post.caption_entity_urls,
        origin_title=origin_title,
        origin_username=origin_username,
        author_first_name=author_first_name,
        author_username=author_username,
        postmail_chat_link=settings.postmail_chat_link,
        hidden_author_names=settings.publisher_hidden_author_names,
    )
    markup = _markup(buttons)

    caption_new = publisher.emojis_to_numbers(caption)
    caption_pt = await _translate(caption_new, "Brazilian Portuguese", group_id=group_id)
    caption_en = await _translate(caption_new, "English", group_id=group_id)

    rates = _RateLookup()
    await _warm_rates(rates, caption_pt, "BRL")
    await _warm_rates(rates, caption_en, "USD")
    caption_pt = publisher.finalise_caption(
        publisher.convert_prices_in_text(caption_pt, "BRL", rates)
    )
    caption_en = publisher.finalise_caption(
        publisher.convert_prices_in_text(caption_en, "USD", rates)
    )

    chat_id = settings.postmail_chat_id
    sent_pt = await _send_media(bot, chat_id, post, caption_pt, markup)
    sent_en = await _send_media(bot, chat_id, post, caption_en, markup)
    return sent_pt, sent_en


async def _warm_rates(rates: _RateLookup, text: str, code_target: str) -> None:
    """Prefetch every rate `convert_prices_in_text` could ask for.

    Runs the converter once against a lookup that records misses instead of
    answering them, then fetches each recorded pair. Cheaper than making the
    pure function async, and it keeps `cb_core.publisher` free of I/O.
    """
    wanted: set[tuple[str, str]] = set()

    def record(code_from: str, target: str) -> float | None:
        wanted.add((code_from, target))
        return None

    publisher.convert_prices_in_text(text, code_target, record)
    for code_from, target in wanted:
        await rates.warm(code_from, target)


def _markup(buttons: list[publisher.PostButton]) -> Any:
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=b.text, url=b.url)] for b in buttons]
    )


async def _send_media(
    bot: Bot, chat_id: int, post: pending_posts.PendingPost, caption: str, markup: Any
) -> int:
    """v1's three send branches (`:210-218`).

    `parse_mode="HTML"` on the photo send only. v1 passes it to `sendPhoto` and
    omits it from `sendVideo`/`sendAnimation`, so a video ad's caption renders
    its tags literally — D-PF-5, preserved, because changing it would alter how
    half the network's existing posts look.
    """
    if post.media_kind == "photo":
        sent = await bot.send_photo(
            chat_id, post.file_id, caption=caption, reply_markup=markup, parse_mode="HTML"
        )
    elif post.media_kind == "video":
        sent = await bot.send_video(chat_id, post.file_id, caption=caption, reply_markup=markup)
    else:
        sent = await bot.send_animation(chat_id, post.file_id, caption=caption, reply_markup=markup)
    return sent.message_id


# --------------------------------------------------------------------- the fan-out

# One cross-shard read, in a scheduled job — AGENTS.md §4.4's sanctioned case.
# It replaces v1's per-group backend `configs/{id}` round trip *and* its
# per-group `getChat` for the title (`:246,260`), which is why `groups.title` is
# read here rather than asked of Telegram.
_TARGETS = """
SELECT g.group_id,
       coalesce(g.title, '')                      AS title,
       coalesce(gc.publisher_post, false)         AS publisher_post,
       coalesce(gc.sfw, true)                     AS sfw,
       coalesce(gc.language, 'en')                AS language,
       coalesce(gc.publisher_members_only, false) AS publisher_members_only,
       coalesce(gc.max_posts, 9999)               AS max_posts
  FROM groups g
  LEFT JOIN group_configs gc USING (group_id)
 WHERE g.left_at IS NULL
 ORDER BY g.group_id
"""


def _next_run_at(hour: int, minute: int) -> datetime:
    """v1's `create_job` (`:96`): today at `hour:minute`, plus one day —
    unconditionally, so the first delivery is always tomorrow even when the
    chosen time is still ahead today."""
    local = Instant.now().to_system_tz()
    return (
        local.replace(hour=hour, minute=minute, second=0, nanosecond=0).add(days=1).to_instant()
    ).to_stdlib()


async def _fan_out(
    bot: Bot,
    *,
    origin_title: str,
    author_username: str | None,
    days: int,
    has_nsfw: bool,
    requester_chat_id: int,
    requester_message_id: int,
    requester_user_id: int,
    sent_pt: int,
    sent_en: int,
) -> str:
    """`schedule_post`'s per-group loop (`:238-278`), returning v1's report."""
    settings = get_settings()
    lines = [f"Post set for the following times ({days} days):", "NOW - Cookiebot Mural 📬"]

    for row in await db.fetch(_TARGETS, name="publisher_targets"):
        group_id = int(row["group_id"])
        try:
            if not row["publisher_post"]:
                publisher_fanout_total.labels(outcome="skipped").inc()
                continue
            if has_nsfw and row["sfw"]:
                publisher_fanout_total.labels(outcome="skipped").inc()
                continue
            if row["publisher_members_only"] and not await _author_is_member(
                group_id, author_username
            ):
                publisher_fanout_total.labels(outcome="skipped").inc()
                continue

            # v1 kept one live campaign per source channel per target (`:238-242`)
            # by deleting matching rows before inserting. Same rule, as a
            # single-shard statement rather than a scan of every row.
            await scheduled_posts.delete_by_origin_title(group_id, origin_title)
            await scheduled_posts.trim_to_max(group_id, int(row["max_posts"]))

            hour = random.randint(0, 23)
            minute = random.randint(0, 59)
            await scheduled_posts.create(
                group_id=group_id,
                origin_title=origin_title,
                target_title=row["title"],
                days_remaining=days,
                next_run_at=_next_run_at(hour, minute),
                source_chat_id=settings.postmail_chat_id,
                source_message_id=sent_pt if row["language"] == "pt" else sent_en,
                requester_chat_id=requester_chat_id,
                requester_message_id=requester_message_id,
                requester_user_id=requester_user_id,
            )
            lines.append(f"{hour}:{minute} - {row['title']}")
            publisher_fanout_total.labels(outcome="scheduled").inc()
        except Exception as exc:  # noqa: BLE001 - v1's per-group `except: pass` (`:275-276`)
            # v1 swallowed this silently, so a group vanished from the schedule
            # with no trace anywhere. Same outcome for the user, now logged.
            log.warning("publisher.fanout_group_failed", error=str(exc))
            publisher_fanout_total.labels(outcome="error").inc()

    lines.append("OBS: private chats are not listed!")
    return "\n".join(lines)


async def _author_is_member(group_id: int, author_username: str | None) -> bool:
    """v1's `publisher_members_only` check (`:251-257`).

    v1 stringified the whole member register and ran `username not in str(members)`
    — a substring test, so an author called `bob` passed the check in any group
    containing a `bobby`. That is a bug rather than a behaviour; this compares
    set membership (D-PF-12). v1 also skipped the group on any exception, and an
    empty roster here reaches the same outcome.
    """
    if not author_username:
        return False
    roster = await members.roster(group_id)
    return any(ref.username == author_username for ref in roster)


# ------------------------------------------------------------------------- the jobs


async def publisher_approve(
    ctx: dict[str, Any],
    *,
    pending_key: str,
    origin_chat_id: int,
    requester_chat_id: int,
    requester_message_id: int,
    requester_user_id: int,
    days: int,
    has_nsfw: bool,
) -> None:
    """`schedule_post` (`:230-286`): render into the Mural, then fan out."""
    parent = context_from_carrier(ctx.get("trace_carrier"))
    start = time.perf_counter()
    outcome = "ok"
    token = None
    try:
        from opentelemetry import context as otel_context

        token = otel_context.attach(parent)
        with span("job.publisher_approve", kind=SpanKind.CONSUMER):
            await _run_approve(
                ctx["bot"],
                pending_key=pending_key,
                origin_chat_id=origin_chat_id,
                requester_chat_id=requester_chat_id,
                requester_message_id=requester_message_id,
                requester_user_id=requester_user_id,
                days=days,
                has_nsfw=has_nsfw,
            )
    except Exception:
        outcome = "error"
        log.exception("job.failed", job="publisher_approve")
        raise
    finally:
        if token is not None:
            from opentelemetry import context as otel_context

            otel_context.detach(token)
        job_duration.labels(job="publisher_approve", outcome=outcome).observe(
            time.perf_counter() - start
        )


async def _run_approve(
    bot: Bot,
    *,
    pending_key: str,
    origin_chat_id: int,
    requester_chat_id: int,
    requester_message_id: int,
    requester_user_id: int,
    days: int,
    has_nsfw: bool,
) -> None:
    settings = get_settings()
    # Read, don't consume. v1's `prepare_post` pops the cache entry as its last
    # act (`:219-220`), which is fine for a function that cannot be retried —
    # but this is an arq job, and arq retries. Consuming here would mean a
    # failure anywhere below (a Telegram 5xx mid-upload, a pool blip) leaves the
    # retry with nothing to render: it would answer `publish_expired` and the
    # campaign would be lost silently, having already posted whatever part of
    # the render succeeded. The entry is discarded once the fan-out has
    # committed, so a retry re-renders instead — duplicate Mural posts are
    # visible and recoverable; a silently dropped campaign is neither.
    post = await pending_posts.get(pending_key)
    if post is None:
        # Genuinely gone — a restart before Valkey took the write, or the TTL.
        # v1 raised KeyError into the global traceback handler and told nobody
        # (D-PF-3); the honest answer is to say so where it was submitted.
        log.warning("publisher.pending_missing", key=pending_key)
        await _try_send(bot, requester_chat_id, "publish_expired", reply_to=requester_message_id)
        return

    origin_chat = await bot.get_chat(origin_chat_id)
    origin_title = origin_chat.title or ""
    author_first_name: str | None = None
    author_username: str | None = None
    try:
        member = await bot.get_chat_member(origin_chat_id, requester_user_id)
    except Exception as exc:  # noqa: BLE001 - v1's bare `except` (`:235-236`)
        log.warning("publisher.author_lookup_failed", error=str(exc))
    else:
        author_first_name = member.user.first_name
        author_username = member.user.username

    sent_pt, sent_en = await _render(
        bot,
        post,
        origin_title=origin_title,
        origin_username=origin_chat.username,
        author_first_name=author_first_name,
        author_username=author_username,
        group_id=requester_chat_id,
    )

    report = await _fan_out(
        bot,
        origin_title=origin_title,
        author_username=author_username,
        days=days,
        has_nsfw=has_nsfw,
        requester_chat_id=requester_chat_id,
        requester_message_id=requester_message_id,
        requester_user_id=requester_user_id,
        sent_pt=sent_pt,
        sent_en=sent_en,
    )

    # The rows are committed; nothing below can fail in a way a retry would
    # help with, so this is the point the submission stops being pending.
    await pending_posts.discard(pending_key)

    # v1 wraps *only* the reporting block, so a DM failure changes the reply and
    # nothing else (`:277-286`). Same boundary here.
    try:
        if settings.owner_id:
            await bot.send_message(settings.owner_id, report)
        await bot.send_message(requester_user_id, report)
    except Exception as exc:  # noqa: BLE001 - "the user has never DM'd the bot" is routine
        log.warning("publisher.report_dm_failed", error=str(exc))
        reply_key = "publish_queued_no_dm"
    else:
        reply_key = "publish_queued"
    await _try_send(bot, requester_chat_id, reply_key, reply_to=requester_message_id)


async def _try_send(bot: Bot, chat_id: int, key: str, *, reply_to: int | None = None) -> None:
    """Best-effort localised reply. The job has already done its work by the
    time these run, so a send failure must not fail the job and retry it — that
    would render and schedule the whole campaign a second time."""
    from cb_core.locales import get as locale_get

    try:
        await bot.send_message(chat_id, locale_get(key, "en"), reply_to_message_id=reply_to)
    except Exception as exc:  # noqa: BLE001 - see docstring
        log.warning("publisher.reply_failed", key=key, error=str(exc))


# ---------------------------------------------------------------------- the delivery


async def deliver_scheduled_posts(ctx: dict[str, Any]) -> int:
    """`scheduler_pull` (`:329-357`), as a five-minute cron. Returns rows sent."""
    parent = context_from_carrier(ctx.get("trace_carrier"))
    start = time.perf_counter()
    outcome = "ok"
    token = None
    try:
        from opentelemetry import context as otel_context

        token = otel_context.attach(parent)
        with span("job.deliver_scheduled_posts", kind=SpanKind.CONSUMER):
            return await _run_delivery(ctx["bot"])
    except Exception:
        outcome = "error"
        log.exception("job.failed", job="deliver_scheduled_posts")
        raise
    finally:
        if token is not None:
            from opentelemetry import context as otel_context

            otel_context.detach(token)
        job_duration.labels(job="deliver_scheduled_posts", outcome=outcome).observe(
            time.perf_counter() - start
        )


async def _run_delivery(bot: Bot) -> int:
    from cb_core import group_config

    now = Instant.now()
    due = await scheduled_posts.due_before(now.to_stdlib())
    sent = 0
    for post in due:
        # v1 spends a day on every attempt, before the send and regardless of
        # whether it succeeds (`:335-339`). D-PF-9, preserved: the alternative
        # is retrying a permanently broken target until the heat death.
        await scheduled_posts.advance_or_expire(post, now.add(hours=24).to_stdlib())

        config = await group_config.get_config(post.group_id)
        if not config.publisher_post:
            # v1 deletes rather than pauses (`:342-345`). D-PG-4, preserved:
            # withdrawal of consent is final, not a hold.
            await scheduled_posts.delete(post.group_id, post.post_id)
            publisher_delivery_total.labels(outcome="opted_out").inc()
            continue

        try:
            thread_id = await _thread_id(bot, post.group_id, config.thread_posts)
            await bot.forward_message(
                post.group_id,
                post.source_chat_id,
                post.source_message_id,
                message_thread_id=thread_id,
            )
        except TelegramForbiddenError as exc:
            # v1's `BotWasKickedError` branch (`:352-354`).
            log.info("publisher.delivery_forbidden", error=str(exc))
            await scheduled_posts.delete(post.group_id, post.post_id)
            publisher_delivery_total.labels(outcome="dropped").inc()
        except TelegramBadRequest as exc:
            # A chat or message that no longer exists is permanent; anything
            # else Telegram calls a bad request is not worth a second guess.
            log.info("publisher.delivery_rejected", error=str(exc))
            await scheduled_posts.delete(post.group_id, post.post_id)
            publisher_delivery_total.labels(outcome="dropped").inc()
        except Exception as exc:  # noqa: BLE001 - transient; the next tick retries
            # v1 deleted the row here too (`:355-357`), so one 5xx ended the
            # campaign for that group. D-PF-8: leave it alone. `days_remaining`
            # has already been spent above, so a permanently broken target still
            # drains on its original schedule rather than living forever.
            log.warning("publisher.delivery_failed", error=str(exc))
            publisher_delivery_total.labels(outcome="error").inc()
        else:
            sent += 1
            publisher_delivery_total.labels(outcome="sent").inc()
    return sent


async def _thread_id(bot: Bot, group_id: int, thread_posts: str | None) -> int | None:
    """The forum topic to deliver into, or `None` for the General thread.

    v1 passed `int(config[10])` whenever the chat reported `is_forum`
    (`:348-349`) — including its own `"9999"` sentinel for "no topic set", so a
    forum group that never configured one got a forward into topic 9999, which
    fails, which `scheduler_pull`'s catch-all then punished by deleting the row.
    D-PG-1: v2 normalises that sentinel to NULL at the storage layer
    (`group_config.py:66-69`), and NULL means no argument at all.
    """
    if not thread_posts:
        return None
    try:
        chat = await bot.get_chat(group_id)
    except Exception as exc:  # noqa: BLE001 - not knowing means the General thread
        log.warning("publisher.chat_lookup_failed", error=str(exc))
        return None
    if not getattr(chat, "is_forum", False):
        return None
    try:
        return int(thread_posts)
    except ValueError:
        log.warning("publisher.bad_thread_id", value=thread_posts)
        return None


__all__ = [
    "deliver_scheduled_posts",
    "publisher_approve",
    "publisher_delivery_total",
    "publisher_fanout_total",
    "set_http_client",
]
