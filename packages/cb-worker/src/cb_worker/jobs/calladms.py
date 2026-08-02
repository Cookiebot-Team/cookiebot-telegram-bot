"""`/adm`'s DM half — design R3. v1: `call_admins`, `UserRegisters.py:178-203`,
the DM loop specifically at `:190-203` (the group ping is
`cb_gateway/handlers/calladms.py`; this is only what happens per admin
afterwards).

Deliberately does not import `cb_worker.main`: `main.py` imports this module
to register it in `WorkerSettings.functions`, and a module a package
registers must not import back from the thing registering it. The telemetry
shape below (span, `job_duration`, the `job.failed` log on an unexpected
raise) mirrors `cb_worker/jobs/everyone.py`'s own wrapper — copied rather
than imported, same reasoning: no shared wrapper module exists for two call
sites, and each job module stays self-contained.

v1 re-resolved each admin's id with one backend `GET users?username=` per
admin (`:191`) and skipped both an unresolvable username and the bot's own
id. v2 has no username-keyed lookup at all: `cb_core.admins.admin_ids`
already returns ids directly, cached and outage-resilient, so only the
bot-id exclusion remains — and `aiogram.Bot.id` needs no API call, being
derived from the token itself.

**The every-10th-DM forward to a hardcoded bot-owner id is not ported**
(`UserRegisters.py:195-196`). Same reasoning as `util_everyone`'s D-EV-5:
undisclosed exfiltration of group content, no configuration, no v2 concept
of an owner id to forward to.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from opentelemetry.trace import SpanKind
from prometheus_client import Counter

from cb_core import admins
from cb_core.locales import get as locale_get
from cb_core.logging import get_logger
from cb_core.metrics import job_duration
from cb_core.telemetry import context_from_carrier, span

log = get_logger("cb.worker.calladms")

# outcome in sent|blocked. Never a group/user id label (AGENTS.md §7) —
# per-group and per-admin detail comes from the structlog event below, whose
# fields logs may carry but metrics may not.
calladms_dm_total = Counter(
    "cb_worker_calladms_dm_total", "DMs attempted by /adm's admin notify fan-out", ["outcome"]
)

# v1: `InlineKeyboardButton(text="Show message", url=...)` (`UserRegisters.py:199`).
# Hardcoded English, never routed through `i18n.get` — reproduced verbatim,
# unlocalised, same as `cb_worker/jobs/everyone.py`'s identical button.
_BUTTON_LABEL = "Show message"


def _deep_link(group_id: int, message_id: int) -> str:
    """v1: `f"https://t.me/c/{str(chat['id']).replace('-100', '')}/{message_id}"`
    (`UserRegisters.py:199`). `removeprefix` rather than `.replace`, same
    correction `everyone.py`'s `_deep_link` already makes for the identical
    v1 expression: `.replace` would also mangle a `-100` occurring anywhere
    else in the id string, not only the supergroup marker at the front.
    """
    return f"https://t.me/c/{str(group_id).removeprefix('-100')}/{message_id}"


async def notify_admins_of_call(
    ctx: dict[str, Any],
    *,
    group_id: int,
    chat_title: str,
    original_message_id: int,
    lang: str,
) -> None:
    """Re-resolves admins rather than trusting a list shipped through the
    queue (design R2.2): this job is not latency-sensitive, so the extra
    cached Telegram round trip through `cb_core.admins` is the correct
    trade-off, and a button pressed minutes ago should DM whoever is an
    admin *now*, not whoever was one when it was pressed.

    Each send is individually suppressed — "blocked by user" or "chat never
    started" is the routine outcome, not an error (v1's bare `except`,
    `:202-203`) — and a raise from one admin's `send_message` must never
    abort the loop for the rest (v1 ran this per-admin try/except-guarded
    too).
    """
    parent = context_from_carrier(ctx.get("trace_carrier"))
    start = time.perf_counter()
    outcome = "ok"
    token = None
    try:
        from opentelemetry import context as otel_context

        token = otel_context.attach(parent)
        with span("job.notify_admins_of_call", kind=SpanKind.CONSUMER):
            sent = await _notify(ctx["bot"], group_id, chat_title, original_message_id, lang)
        log.info("calladms.notify_done", group_id=group_id, sent=sent)
    except Exception:
        outcome = "error"
        log.exception("job.failed", job="notify_admins_of_call")
        raise
    finally:
        if token is not None:
            from opentelemetry import context as otel_context

            otel_context.detach(token)
        job_duration.labels(job="notify_admins_of_call", outcome=outcome).observe(
            time.perf_counter() - start
        )


async def _notify(
    bot: Bot, group_id: int, chat_title: str, original_message_id: int, lang: str
) -> int:
    text = locale_get("notification_admin", lang, title=chat_title)
    # v1's exact substring test (`:199`): a button only for a chat id shaped
    # like a supergroup's, no `reply_markup` at all otherwise — not aiogram's
    # own "-100 prefix" convention, which `everyone.py`'s DM always assumes.
    keyboard = None
    if "-100" in str(group_id):
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=_BUTTON_LABEL, url=_deep_link(group_id, original_message_id)
                    )
                ]
            ]
        )

    sent = 0
    # Sorted for deterministic tests and log ordering; DM order carries no
    # user-visible meaning, each is an independent private chat.
    for admin_id in sorted(await admins.admin_ids(bot, group_id)):
        if admin_id == bot.id:
            # v1: `int(user[0]['id']) == int(myself['id'])` (`:192`). `bot.id`
            # is derived from the token, no API call needed.
            continue
        try:
            await bot.send_message(admin_id, text, parse_mode="HTML", reply_markup=keyboard)
        except Exception as exc:  # noqa: BLE001 - v1's bare except: blocked-by-user is routine
            log.warning("calladms.notify", group_id=group_id, outcome="blocked", error=str(exc))
            calladms_dm_total.labels(outcome="blocked").inc()
        else:
            sent += 1
            calladms_dm_total.labels(outcome="sent").inc()

        await asyncio.sleep(0.1)
    return sent


__all__ = ["calladms_dm_total", "notify_admins_of_call"]
