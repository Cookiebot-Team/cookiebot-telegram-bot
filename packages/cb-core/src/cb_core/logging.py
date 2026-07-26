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

    # `PrintLoggerFactory` writes straight to stdout and never touches stdlib
    # logging, which is faster and is what every deployment gets. But an OTLP
    # handler is a *stdlib* handler, so with that factory it would never see a
    # single structlog line — the log pipeline would look wired and ship
    # nothing but third-party chatter. Routing through stdlib when (and only
    # when) OTLP logs are on is what actually connects the two; stdout is
    # unchanged either way, since `basicConfig` below renders `%(message)s`.
    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=(
            structlog.stdlib.LoggerFactory()
            if settings.otlp_logs_enabled
            else structlog.PrintLoggerFactory(file=sys.stdout)
        ),
        cache_logger_on_first_use=True,
    )
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)
    # These are loud and say nothing we do not already trace.
    for noisy in ("asyncio", "aiogram.event", "httpx", "hpack"):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))

    if settings.otlp_logs_enabled:
        _attach_otlp_log_handler(settings, level)


def _attach_otlp_log_handler(settings: Settings, level: int) -> None:
    """Also ship log records to the collector, which forwards them to Loki.

    stdout stays the primary sink — this is an *addition*, never a
    replacement. A log line is the one signal people reach for when something
    is already wrong, and routing it exclusively through a network hop means
    the outage that broke the collector also erased the evidence.

    What this buys: `_add_trace_context` above already stamps every line with
    `trace_id`/`span_id`, and the OTLP record carries the active span context
    natively, so a span in Tempo links to the exact lines emitted inside it.
    Without this the log store only ever sees what a shipper scraped off
    stdout, with no trace correlation at all.

    Import-time failures degrade to a warning: an observability sink is never
    worth taking the service down for.
    """
    try:
        from opentelemetry._logs import set_logger_provider
        from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.sdk.resources import Resource
    except ImportError as exc:  # pragma: no cover - depends on the install extra
        logging.getLogger("cb.logging").warning("otlp logs unavailable: %s", exc)
        return

    provider = LoggerProvider(
        resource=Resource.create(
            {
                "service.name": settings.service_name,
                "service.namespace": "cookiebot",
                "deployment.environment": settings.env,
            }
        )
    )
    provider.add_log_record_processor(
        BatchLogRecordProcessor(OTLPLogExporter(endpoint=settings.otlp_endpoint, insecure=True))
    )
    set_logger_provider(provider)
    # On the root logger, so it catches both halves: structlog's own lines
    # (routed through stdlib by the factory switch in `configure_logging`) and
    # every stdlib logger in the process, including the ones inside aiogram.
    logging.getLogger().addHandler(LoggingHandler(level=level, logger_provider=provider))


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
