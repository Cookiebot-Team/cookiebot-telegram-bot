"""`/everyone`'s DM fan-out — design R5. v1: `UserRegisters.py:121-146`, the tail
half of `call_everyone` (the group ping is `cb_gateway/handlers/everyone.py`,
design R4; this is only what happens per member afterwards).

Deliberately does not import `cb_worker.main`: `main.py` imports this module to
register it in `WorkerSettings.functions` (design R5.1), and a module a package
registers must not import back from the thing registering it. The telemetry
shape below (span, `job_duration`, the `job.failed` log on an unexpected raise)
mirrors the wrapper the cron jobs in `main.py` share — copied rather than
imported, for that reason.

v1 re-validated membership with one backend `GET users?username=` per member
(`:129`) and, on either a bad lookup or a live `left`/`kicked` status, deleted
the username from the register (`:134`, `DELETE registers/{chat_id}/users`).
v2's roster already carries `user_id`, so only the live Telegram check remains;
a `left`/`kicked` member is marked left (`members.mark_left`) rather than
deleted, so `first_seen_at` survives a rejoin (design R5.2, open decision 4).

**D-EV-5 is not ported.** v1 forwarded the triggering group message to a
hardcoded owner id every 10th successful DM (`:137-138`) — undisclosed
exfiltration of group content, no configuration, no user-facing trace. There is
no owner id in v2 and no equivalent here.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from opentelemetry.trace import SpanKind
from prometheus_client import Counter

from cb_core import members
from cb_core.locales import get as locale_get
from cb_core.logging import get_logger
from cb_core.metrics import job_duration
from cb_core.telemetry import context_from_carrier, span

log = get_logger("cb.worker.everyone")

# outcome in sent|blocked|left|error. Never a group/user id label (AGENTS.md
# §7) — per-group and per-user detail comes from the structlog event below,
# whose fields logs may carry but metrics may not (design R5.6).
everyone_dm_total = Counter(
    "cb_worker_everyone_dm_total", "DMs attempted by the /everyone fan-out", ["outcome"]
)

# v1: `InlineKeyboardButton(text="Show message", url=...)` (`UserRegisters.py:141`).
# Hardcoded English, never routed through `i18n.get` — reproduced verbatim,
# unlocalised, per design R5.3.
_BUTTON_LABEL = "Show message"


def _deep_link(chat_id: int, message_id: int) -> str:
    """v1: `f"https://t.me/c/{str(chat['id']).replace('-100', '')}/{msg['message_id']}"`
    (`UserRegisters.py:142`). `removeprefix` rather than `.replace`: v1's
    `.replace` would also mangle a `-100` occurring anywhere else in the id
    string, not only the supergroup marker at the front. A bare (non-`-100`)
    chat id — never emitted by Telegram for a group but reachable if this is
    ever called with one — passes through unchanged either way.
    """
    return f"https://t.me/c/{str(chat_id).removeprefix('-100')}/{message_id}"


async def everyone_fanout(
    ctx: dict[str, Any],
    *,
    group_id: int,
    chat_id: int,
    message_id: int,
    chat_title: str,
    lang: str,
) -> None:
    """Re-reads the roster rather than trusting a list shipped through the
    queue (design R4.7/open decision 2): a job that runs late DMs the
    membership as it is *then*, and the payload the gateway enqueued stays a
    handful of scalars.

    Each send is individually suppressed — "blocked by user" is the routine
    outcome, not an error (v1's bare `except`, `:145-146`) — and a raise from
    one member's `get_chat_member` or `send_message` must never abort the loop
    for the rest (v1 ran this per-member try/except-guarded too).
    """
    parent = context_from_carrier(ctx.get("trace_carrier"))
    start = time.perf_counter()
    outcome = "ok"
    token = None
    try:
        from opentelemetry import context as otel_context

        token = otel_context.attach(parent)
        with span("job.everyone_fanout", kind=SpanKind.CONSUMER):
            sent = await _fanout(ctx["bot"], group_id, chat_id, message_id, chat_title, lang)
        log.info("everyone.fanout", group_id=group_id, sent=sent)
    except Exception:
        outcome = "error"
        log.exception("job.failed", job="everyone_fanout")
        raise
    finally:
        if token is not None:
            from opentelemetry import context as otel_context

            otel_context.detach(token)
        job_duration.labels(job="everyone_fanout", outcome=outcome).observe(
            time.perf_counter() - start
        )


async def _fanout(
    bot: Bot, group_id: int, chat_id: int, message_id: int, chat_title: str, lang: str
) -> int:
    text = locale_get("everyone_call", lang, title=chat_title)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=_BUTTON_LABEL, url=_deep_link(chat_id, message_id))]
        ]
    )
    sent = 0
    for member in await members.roster(group_id):
        try:
            chat_member = await bot.get_chat_member(chat_id, member.user_id)
        except Exception as exc:  # noqa: BLE001 - one member's failure must not abort the fan-out
            log.warning("everyone.fanout", group_id=group_id, outcome="error", error=str(exc))
            everyone_dm_total.labels(outcome="error").inc()
            continue

        if chat_member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
            await members.mark_left(group_id, member.user_id)
            everyone_dm_total.labels(outcome="left").inc()
            continue

        try:
            await bot.send_message(member.user_id, text, parse_mode="HTML", reply_markup=keyboard)
        except Exception as exc:  # noqa: BLE001 - v1's bare except: blocked-by-user is routine
            log.warning("everyone.fanout", group_id=group_id, outcome="blocked", error=str(exc))
            everyone_dm_total.labels(outcome="blocked").inc()
        else:
            sent += 1
            everyone_dm_total.labels(outcome="sent").inc()

        await asyncio.sleep(0.1)
    return sent


__all__ = ["everyone_dm_total", "everyone_fanout"]
