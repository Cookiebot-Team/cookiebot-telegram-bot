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

from cb_core import cache, errors, groups, locales, metrics, tenancy
from cb_core.dedupe import RecentIds, idempotency_key
from cb_core.events import recorder
from cb_core.logging import get_logger
from cb_core.telemetry import current_trace_id, record_error, span
from cb_core.textmatch import ParsedCommand, parse_command
from cb_gateway.command_catalog import command_blocked_for_tenant, fetch_catalog_row
from cb_gateway.telemetry import OUTCOME_ATTR, error_reason_for_chat, mark_outcome

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


async def _tell_user(update: Update, group_id: int, exc: BaseException | None = None) -> None:
    """Answer a failed update with what went wrong and its trace id.

    Before this the user got nothing at all — the handler raised, the gateway
    still returned 200 so Telegram would not redeliver, and the bot simply went
    quiet. "It ignored me" and "it broke" look identical from the chat, and
    neither gives anyone a way to find the failure in the logs.

    Two things go in the message. The **reason** is `errors.reason(exc)`: the
    innermost failure, which is the only link in the chain a person in a chat
    can act on ("Bad Request: can't parse entities…" tells an admin their
    welcome text is the problem; "CbError: welcome.prompt(...)" does not). The
    **trace id** is the join key for everyone else: every log line carries it
    (cb_core.logging), every span carries it, and the Loki datasource links it
    through to Tempo, so pasting it into the Errors dashboard narrows hours of
    traffic to one interaction.

    The reason is HTML-escaped. It is an exception message — Postgres puts
    quoted identifiers in it, Telegram puts the offending markup in it — and it
    is rendered inside a `<blockquote>`, so an unescaped `<` would fail to send
    the very message that explains a failure to send a message.

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
        await reply(
            locales.get(
                "handler_error",
                lang,
                trace=trace,
                reason=error_reason_for_chat(exc),
            )
        )
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
        #
        # This covers the chat an update arrived *in*, which is not always the
        # group it writes to — the config menu runs in the admin's DM and names
        # its group in the prompt text. `cb_core.group_config.set_config`
        # ensures the row for that case; this one is what gives the row a title
        # (a DM knows no group's title) and what covers the other seven tables
        # whose writes only ever happen in-chat.
        if group_id:
            # A CallbackQuery has no `chat` of its own — its chat is the one the
            # message it was attached to lives in. Without the fallback, a group
            # whose first interaction is a button press gets a row with no title.
            chat = getattr(update.event, "chat", None) or getattr(
                getattr(update.event, "message", None), "chat", None
            )
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
                # The exception *type* is the metric's label, and after
                # `errors.fail_as` the outermost type is always `CbError` —
                # which would collapse every failure in the bot into one series.
                # The innermost is what differs between a Telegram rejection and
                # a foreign-key violation, so that is what gets counted.
                innermost = errors.root(exc) or exc
                metrics.handler_errors_total.labels(
                    handler=command or utype, exc_type=type(innermost).__name__
                ).inc()
                # `log.exception` carries the traceback; `error_chain` carries
                # what the traceback cannot — which group, which column, which
                # job — as data a Loki query can filter on rather than prose to
                # read. Both, because they answer different questions.
                log.exception(
                    "handler.failed",
                    command=command,
                    skin=skin,
                    error=errors.render(exc),
                    error_chain=errors.chain(exc),
                )
                await _tell_user(update, group_id, exc)
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


class TenantCommandGateMiddleware(BaseMiddleware):
    """Dispatch-level enforcement of `tenant.disabled_commands` — the gap
    `.specs/features/platform_tenancy/spec.md` named as the one most likely to
    surprise a tenant admin: before this, `disabled_commands` was consulted in
    exactly one place (`listcommand.py`, to hide a command from `/commands`'
    own listing), so a "disabled" command still ran for anyone who typed it.

    Registered last of the three outer middlewares (`main.py`), i.e. innermost
    — it must run *after* `TelemetryMiddleware` has populated
    `data["parsed_command"]`, since it reads that field instead of parsing the
    text a second time. Non-commands (a plain message, a join, a callback with
    no command) leave `parsed_command` `None`, so they fall straight through to
    `handler(event, data)` with no tenant or catalog lookup at all — the
    per-update cost this adds is exactly zero unless the update is a command.

    The catalog fetch is the same seam `/commands` uses, but the *rule* applied
    to its result is `command_blocked_for_tenant`, not the listing's
    `command_available_for_tenant`. Read that function's docstring before
    changing either: listing is an allowlist (advertise only what the catalog
    describes) and dispatch is a denylist (drop only what is explicitly off).
    Collapsing them deletes every command the 29-row seed does not mention —
    `/giveaway`, `/transcribe`, `/destroy`, every owner command — from the bot.

    Fails open: a tenant-registry or catalog outage must run the command, not
    drop it — the same rule `DedupeMiddleware`'s cache branch above follows for
    a Valkey outage, and `core_stickerspam`'s "cache outage fails open not
    closed" scenario. `tenancy.registry.by_skin` already never raises (falls
    back to `tenancy.FALLBACK`); only the catalog read can raise here, same as
    in `listcommand._commands_available`.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        parsed: ParsedCommand | None = data.get("parsed_command")
        if parsed is None:
            return await handler(event, data)

        skin: str = data.get("skin", tenancy.DEFAULT_TENANT)
        try:
            tenant = await tenancy.registry.by_skin(skin)
            row = await fetch_catalog_row(parsed.name)
        except Exception as exc:  # noqa: BLE001 - a lookup outage must not drop a command
            log.warning("tenant_gate.lookup_failed", skin=skin, command=parsed.name, error=str(exc))
            return await handler(event, data)

        if not command_blocked_for_tenant(parsed.name, row, tenant):
            return await handler(event, data)

        # Silent, matching how v1's absent handler behaved: a persona that
        # never had a given command simply produced no reply, never an error
        # message (module docstring; do not invent one here).
        metrics.updates_dropped_total.labels(reason="tenant_disabled").inc()
        log.info(
            "tenant_gate.command_disabled",
            skin=skin,
            tenant_id=tenant.tenant_id,
            command=parsed.name,
        )
        mark_outcome("silent")
        return None
