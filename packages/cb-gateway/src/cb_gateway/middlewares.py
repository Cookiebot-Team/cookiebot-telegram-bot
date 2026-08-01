"""Outer middleware: one span, one metric, one analytics row, per update.

Everything cross-cutting lives here so handlers stay pure. v1 had no equivalent —
timing, dedupe and (nonexistent) analytics were scattered through the dispatcher.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any, cast

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, Update
from opentelemetry.trace import SpanKind

from cb_core import cache, groups, locales, metrics
from cb_core.dedupe import RecentIds, idempotency_key
from cb_core.events import recorder
from cb_core.logging import get_logger
from cb_core.telemetry import current_trace_id, record_error, span
from cb_core.textmatch import parse_command
from cb_gateway.telemetry import OUTCOME_ATTR

log = get_logger("cb.gateway.mw")


def _update_type(update: Update) -> str:
    return update.event_type or "unknown"


def _callback_action(data: str | None) -> str:
    """Low-cardinality label for a callback press.

    Every handler's `callback_data` wire shape (`config_menu.build_callback_data`,
    `calladms.build_callback_data`...) is a handful of fixed tokens plus a
    trailing group/message id, e.g. `"k CONFIG 123"` or `"CALLADMS YES 456"`.
    Keeping the non-numeric tokens and dropping the rest names the interaction
    without ever putting that id in a span (AGENTS.md §7's cardinality rule is
    written for metric labels but applies just as much to a span name, which is
    exactly as high-cardinality a place to leak one).
    """
    if not data:
        return "unknown"
    tokens = [tok for tok in data.split() if not tok.lstrip("-").isdigit()]
    return ":".join(tokens).lower() if tokens else "unknown"


def _interaction_name(update: Update, command: str | None) -> str | None:
    """The resolved command or interaction, when this update is one — `None`
    for everything else (a plain message headed for a passive content-rule
    handler), which the caller falls back to naming by raw update type.
    """
    if command:
        return f"telegram.command /{command}"
    event = update.event
    if isinstance(event, CallbackQuery):
        return f"telegram.callback {_callback_action(event.data)}"
    if isinstance(event, Message):
        if event.new_chat_members:
            return "telegram.member_join"
        if event.left_chat_member is not None:
            return "telegram.member_leave"
    return None


def _ids(update: Update) -> tuple[int, int | None]:
    """(group_id, user_id). group_id 0 means a private chat."""
    event = update.event
    if isinstance(event, Message):
        chat_id = event.chat.id if event.chat else 0
        return (chat_id if chat_id < 0 else 0, event.from_user.id if event.from_user else None)
    if isinstance(event, CallbackQuery):
        msg = event.message
        chat_id = msg.chat.id if msg and msg.chat else 0
        return (chat_id if chat_id < 0 else 0, event.from_user.id if event.from_user else None)
    return (0, None)


async def _tell_user(update: Update, group_id: int) -> None:
    """Answer a failed update with its trace id.

    Before this the user got nothing at all — the handler raised, the gateway
    still returned 200 so Telegram would not redeliver, and the bot simply went
    quiet. "It ignored me" and "it broke" look identical from the chat, and
    neither gives anyone a way to find the failure in the logs.

    The trace id is the join key: every log line carries it (cb_core.logging),
    every span carries it, and the Loki datasource links it through to Tempo.
    Pasting it into the Errors dashboard's log panel narrows hours of traffic to
    one interaction.

    Everything here is best-effort. This runs inside the `except` of the failing
    handler and must not replace the original exception with one of its own —
    the raise that follows is what records the failure properly.
    """
    event = getattr(update, "event", None)
    reply = getattr(event, "reply", None) or getattr(event, "answer", None)
    if reply is None:
        return

    trace = current_trace_id()
    if not trace:
        return

    lang = "en"
    if group_id:
        try:
            from cb_core import group_config

            lang = locales.resolve_language((await group_config.get_config(group_id)).language)
        except Exception:  # noqa: BLE001 - a language lookup must not swallow the real error
            pass

    try:
        await reply(locales.get("handler_error", lang, trace=trace))
    except Exception as exc:  # noqa: BLE001 - the chat may be gone, or the bot muted
        log.warning("handler.error_reply_failed", error=str(exc))


class DedupeMiddleware(BaseMiddleware):
    """Telegram redelivers any update we do not 2xx. Drop repeats.

    Two layers: a per-process LRU (cheap, compiled) and a Valkey SET NX so that
    replicas do not both handle the same redelivery. v1's version cleared its whole
    set at 10 000 entries, reopening the duplicate window right after a burst.
    """

    def __init__(self, capacity: int = 65536, ttl_seconds: int = 600) -> None:
        self._local = RecentIds(capacity=capacity)
        self._ttl = ttl_seconds

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        update = cast(Update, event)  # outer middleware on dp.update always gets an Update
        skin: str = data.get("skin", "cookiebot")
        if self._local.seen(update.update_id):
            metrics.updates_dropped_total.labels(reason="duplicate_local").inc()
            return None
        try:
            key = idempotency_key(skin, update.update_id)
            if not await cache.client().set(key, b"1", ex=self._ttl, nx=True):
                metrics.updates_dropped_total.labels(reason="duplicate_shared").inc()
                return None
        except Exception as exc:  # noqa: BLE001
            # Cache down must not stop traffic; the local LRU still covers this replica.
            log.warning("dedupe.cache.unavailable", error=str(exc))
        return await handler(event, data)


class TelemetryMiddleware(BaseMiddleware):
    """Root span + RED metrics + one `message_events` row."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        update = cast(Update, event)  # outer middleware on dp.update always gets an Update
        skin: str = data.get("skin", "cookiebot")
        utype = _update_type(update)
        group_id, user_id = _ids(update)

        text = getattr(update.event, "text", None) or ""
        parsed = parse_command(text, data.get("bot_username", "")) if text else None
        command = parsed.name if parsed else None
        data["parsed_command"] = parsed

        # Everything a group feature writes carries a foreign key to `groups`,
        # and nothing else creates that row at runtime — the only other INSERT
        # is in the v1 importer. Without this, a group the bot was merely added
        # to gets a working menu whose every write is rejected by the FK, which
        # surfaces as a setting that will not save rather than as an error.
        #
        # Idempotent and memoised per process, so this is a set lookup after
        # the first update from a given chat.
        if group_id:
            chat = getattr(update.event, "chat", None)
            await groups.ensure(
                group_id,
                title=getattr(chat, "title", None),
                chat_type=getattr(chat, "type", None),
                skin=skin,
            )

        metrics.updates_total.labels(bot=skin, update_type=utype).inc()
        start = time.perf_counter()
        outcome = "ok"

        # A resolved command/callback/join gets the name a person scanning a
        # trace list actually wants; everything else (a plain message headed for
        # a passive content-rule handler) keeps the old generic name. Only a
        # resolved interaction gets a default `cb.outcome` — see
        # cb_gateway/telemetry.py's module docstring for why "answered" is the
        # right default there and why a passive update gets no guess at all.
        interaction = _interaction_name(update, command)
        with span(
            interaction or f"telegram.update.{utype}",
            kind=SpanKind.SERVER,
            **{
                "telegram.update_id": update.update_id,
                "telegram.skin": skin,
                "telegram.update_type": utype,
                "telegram.command": command,
                "cb.group_id": group_id or None,
                OUTCOME_ATTR: "answered" if interaction else None,
            },
        ) as sp:
            try:
                return await handler(event, data)
            except Exception as exc:
                outcome = "error"
                sp.set_attribute(OUTCOME_ATTR, "error")
                record_error(sp, exc)
                metrics.handler_errors_total.labels(
                    handler=command or utype, exc_type=type(exc).__name__
                ).inc()
                log.exception("handler.failed", command=command, skin=skin)
                await _tell_user(update, group_id)
                raise
            finally:
                elapsed = time.perf_counter() - start
                metrics.handler_duration.labels(
                    handler=utype, command=command or "-", outcome=outcome
                ).observe(elapsed)
                if group_id:
                    recorder().record(
                        group_id=group_id,
                        event_type="command" if command else utype,
                        user_id=user_id,
                        command=command,
                        outcome=outcome,
                        latency_ms=int(elapsed * 1000),
                        handler=utype,
                        skin=skin,
                    )
