"""`/broadcast` — the owner's message to every group.

v1: `broadcast_message`, `../COOKIEBOT-Telegram-Group-Bot/Bot/Miscellaneous.py:114-122`,
dispatched from the owner's private chat (`COOKIEBOT.py:104-105`):

    for group in groups:
        try:
            send_message(cookiebot, int(group['id']), msg['text'].replace('/broadcast', ''))
            time.sleep(0.5)
        except Exception:
            pass

That is FEATURE-MAP **D8** — a `sleep()` loop over every group on a handler
thread — plus a bare `except: pass` that reports nothing back, so an owner
broadcasting to a thousand groups blocked a thread for eight minutes and had
no idea how many arrived.

Here the sweep enqueues one deferred send per group (`_defer_by`, arq's own
spacing, durable in Redis), the same shape `util_birthday`'s daily broadcast
uses, and the owner gets a count back immediately. A group the bot can no
longer post to is counted, not swallowed.

Does not import `cb_worker.main`; the telemetry wrapper is copied from
`youtube.py` for that reason.
"""

from __future__ import annotations

import time
from typing import Any

from aiogram import Bot
from opentelemetry.trace import SpanKind
from prometheus_client import Counter

from cb_core import jobs, ops
from cb_core.logging import get_logger
from cb_core.metrics import job_duration
from cb_core.telemetry import context_from_carrier, span

log = get_logger("cb.worker.broadcast")

# outcome in sent|failed — never a group id (AGENTS.md §7).
broadcast_total = Counter("cb_worker_broadcast_total", "Groups a /broadcast reached", ["outcome"])

#: v1's `time.sleep(0.5)` between sends (`Miscellaneous.py:120`), as spacing
#: rather than blocking. Telegram's own limit is ~30 messages/second overall,
#: so this is v1's conservatism preserved, not a new constraint.
SPACING_SECONDS = 0.5


async def broadcast_to_groups(ctx: dict[str, Any], *, text: str, owner_id: int) -> int:
    """Fan a message out to every group. Returns how many sends were queued."""
    parent = context_from_carrier(ctx.get("trace_carrier"))
    start = time.perf_counter()
    outcome = "ok"
    token = None
    try:
        from opentelemetry import context as otel_context

        token = otel_context.attach(parent)
        with span("job.broadcast_to_groups", kind=SpanKind.CONSUMER):
            return await _fan_out(ctx, text, owner_id)
    except Exception:
        outcome = "error"
        log.exception("job.failed", job="broadcast_to_groups")
        raise
    finally:
        if token is not None:
            from opentelemetry import context as otel_context

            otel_context.detach(token)
        job_duration.labels(job="broadcast_to_groups", outcome=outcome).observe(
            time.perf_counter() - start
        )


async def _fan_out(ctx: dict[str, Any], text: str, owner_id: int) -> int:
    group_ids = await ops.all_group_ids()
    queued = 0
    for index, group_id in enumerate(group_ids):
        try:
            await ctx["redis"].enqueue_job(
                jobs.BROADCAST_DELIVER,
                group_id=group_id,
                text=text,
                _defer_by=index * SPACING_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 - one group must not end the fan-out
            log.warning("broadcast.enqueue_failed", group_id=group_id, error=str(exc))
            continue
        queued += 1

    bot: Bot = ctx["bot"]
    try:
        # v1 tells the owner nothing at all. A fan-out they cannot see the
        # size of is one they cannot tell went wrong.
        await bot.send_message(owner_id, f"Broadcasting to {queued} of {len(group_ids)} groups.")
    except Exception as exc:  # noqa: BLE001 - the fan-out is queued either way
        log.warning("broadcast.owner_reply_failed", error=str(exc))
    log.info("broadcast.queued", groups=queued)
    return queued


async def deliver_broadcast(ctx: dict[str, Any], *, group_id: int, text: str) -> None:
    """One group's copy. Its own job so a chat the bot was removed from costs
    exactly one failed send, not the rest of the fan-out."""
    bot: Bot = ctx["bot"]
    try:
        await bot.send_message(group_id, text)
    except Exception as exc:  # noqa: BLE001 - v1's own `except: pass`, but counted
        log.info("broadcast.send_failed", group_id=group_id, error=str(exc))
        broadcast_total.labels(outcome="failed").inc()
        return
    broadcast_total.labels(outcome="sent").inc()


__all__ = ["SPACING_SECONDS", "broadcast_to_groups", "broadcast_total", "deliver_broadcast"]
