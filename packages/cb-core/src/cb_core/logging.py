"""structlog JSON logging with trace correlation.

v1 had default Spring/print logging, no correlation IDs (FEATURE-MAP §Observability).
Here every line carries trace_id/span_id, so a log line, a metric exemplar and a
Tempo trace all join on the same key.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from typing import Any

import orjson
import structlog
from opentelemetry import trace
from structlog.typing import Processor

from cb_core.settings import Settings


def _add_trace_context(
    _logger: Any, _name: str, event: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    # `Any`: structlog processor signature; _logger is whatever the wrapped factory produced.
    span = trace.get_current_span()
    ctx = span.get_span_context()
    if ctx.is_valid:
        event["trace_id"] = format(ctx.trace_id, "032x")
        event["span_id"] = format(ctx.span_id, "016x")
    return event


def _orjson_dumps(obj: Any, **_: Any) -> str:
    # `Any`: structlog's serializer contract - arbitrary event dict plus ignored kwargs.
    return orjson.dumps(obj).decode()


def configure_logging(settings: Settings) -> None:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _add_trace_context,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    renderer = (
        structlog.processors.JSONRenderer(serializer=_orjson_dumps)
        if settings.log_json
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)
    # These are loud and say nothing we do not already trace.
    for noisy in ("asyncio", "aiogram.event", "httpx", "hpack"):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
