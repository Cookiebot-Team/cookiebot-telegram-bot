"""OpenTelemetry tracing, shared by all three services.

Context propagates gateway -> arq job payload -> worker -> api, so one media
command is a single end-to-end trace. `trace_id` is also written into the
`message_events` analytics table, which is what makes a Grafana row clickable
through to the trace that produced it.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.propagate import extract, inject
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace import Span, SpanKind

from cb_core.settings import Settings

_INITIALISED = False


def setup_tracing(settings: Settings) -> None:
    global _INITIALISED
    if _INITIALISED or not settings.traces_enabled:
        return

    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": settings.service_name,
                "service.version": "0.1.0",
                "deployment.environment": settings.env,
            }
        ),
        sampler=ParentBased(TraceIdRatioBased(settings.trace_sample_ratio)),
    )
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otlp_endpoint, insecure=True))
    )
    trace.set_tracer_provider(provider)

    # Auto-instrument the libraries that actually do IO.
    from opentelemetry.instrumentation.asyncpg import AsyncPGInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.redis import RedisInstrumentor

    AsyncPGInstrumentor().instrument()
    HTTPXClientInstrumentor().instrument()
    RedisInstrumentor().instrument()

    _INITIALISED = True


def get_tracer(name: str) -> trace.Tracer:
    return trace.get_tracer(name)


def current_trace_id() -> str | None:
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else None


@contextmanager
def span(name: str, kind: SpanKind = SpanKind.INTERNAL, **attrs: Any) -> Iterator[Span]:
    # `Any`: arbitrary span attributes (string, bool, int, float, or sequences thereof).
    tracer = trace.get_tracer("cb")
    with tracer.start_as_current_span(name, kind=kind) as sp:
        for k, v in attrs.items():
            if v is not None:
                sp.set_attribute(k, v)
        yield sp


def carrier_from_context() -> dict[str, str]:
    """W3C traceparent to embed in an arq job payload."""
    carrier: dict[str, str] = {}
    inject(carrier)
    return carrier


def context_from_carrier(carrier: dict[str, str] | None) -> Context:
    """Restore the parent context on the worker side."""
    return extract(carrier or {})


def record_error(sp: Span, exc: BaseException) -> None:
    sp.record_exception(exc)
    sp.set_status(trace.Status(trace.StatusCode.ERROR, str(exc)))
