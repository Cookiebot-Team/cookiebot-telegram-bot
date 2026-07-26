"""Analytics event emission — the reason Citus is in this stack.

Every update writes one `message_events` row. Rows are buffered in-process and
flushed with a single batched INSERT, because a per-message round trip would put
Postgres on the reply hot path. Loss window on a hard kill is one flush interval,
which is acceptable for analytics and never for state.

`trace_id` is stored on the row, so a slow-command row in Grafana links directly
to its Tempo trace.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import msgspec
from whenever import Instant

from cb_core import db, metrics
from cb_core.logging import get_logger
from cb_core.telemetry import current_trace_id

log = get_logger("cb.events")

_INSERT = """
INSERT INTO message_events (
    ts, group_id, user_id, bot_id, event_type, command, outcome,
    latency_ms, handler, media_kind, llm_tokens, llm_cost_usd, trace_id, attrs
) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
"""


class MessageEvent(msgspec.Struct, omit_defaults=True):
    ts: Any
    group_id: int
    user_id: int | None = None
    bot_id: int | None = None
    event_type: str = "message"
    command: str | None = None
    outcome: str = "ok"
    latency_ms: int | None = None
    handler: str | None = None
    media_kind: str | None = None
    llm_tokens: int | None = None
    llm_cost_usd: float | None = None
    trace_id: str | None = None
    attrs: dict[str, Any] | None = None


class EventRecorder:
    """Batching writer. One per process; started and stopped with the service."""

    def __init__(self, flush_interval: float = 1.0, max_batch: int = 500) -> None:
        self._buf: list[MessageEvent] = []
        self._flush_interval = flush_interval
        self._max_batch = max_batch
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._stopping = False

    async def start(self) -> None:
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="cb-event-flusher")

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self.flush()

    def record(
        self,
        group_id: int,
        event_type: str,
        *,
        user_id: int | None = None,
        bot_id: int | None = None,
        command: str | None = None,
        outcome: str = "ok",
        latency_ms: int | None = None,
        handler: str | None = None,
        media_kind: str | None = None,
        llm_tokens: int | None = None,
        llm_cost_usd: float | None = None,
        **attrs: Any,  # free-form analytics attrs, stored as opaque jsonb
    ) -> None:
        """Non-blocking. Never raises into a handler — analytics must not break a reply."""
        self._buf.append(
            MessageEvent(
                ts=Instant.now().to_stdlib(),
                group_id=group_id,
                user_id=user_id,
                bot_id=bot_id,
                event_type=event_type,
                command=command,
                outcome=outcome,
                latency_ms=latency_ms,
                handler=handler,
                media_kind=media_kind,
                llm_tokens=llm_tokens,
                llm_cost_usd=llm_cost_usd,
                trace_id=current_trace_id(),
                attrs=attrs or None,
            )
        )
        if len(self._buf) >= self._max_batch:
            asyncio.create_task(self.flush())  # noqa: RUF006

    async def flush(self) -> None:
        async with self._lock:
            if not self._buf:
                return
            batch, self._buf = self._buf, []
            rows = [
                (
                    e.ts,
                    e.group_id,
                    e.user_id,
                    e.bot_id,
                    e.event_type,
                    e.command,
                    e.outcome,
                    e.latency_ms,
                    e.handler,
                    e.media_kind,
                    e.llm_tokens,
                    e.llm_cost_usd,
                    e.trace_id,
                    e.attrs,
                )
                for e in batch
            ]
            try:
                await db.executemany(_INSERT, rows, name="insert_message_events")
            except Exception as exc:  # noqa: BLE001
                # Analytics loss beats dropping traffic. Count it so it is visible.
                metrics.handler_errors_total.labels(
                    handler="event_recorder", exc_type=type(exc).__name__
                ).inc()
                log.warning("events.flush.failed", error=str(exc), dropped=len(rows))

    async def _run(self) -> None:
        while not self._stopping:
            await asyncio.sleep(self._flush_interval)
            await self.flush()


_recorder: EventRecorder | None = None


def recorder() -> EventRecorder:
    global _recorder
    if _recorder is None:
        _recorder = EventRecorder()
    return _recorder
