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
from opentelemetry.sdk.trace.sampling import (
    Decision,
    ParentBased,
    Sampler,
    SamplingResult,
    TraceIdRatioBased,
)
from opentelemetry.trace import Span, SpanKind

from cb_core.settings import Settings

_INITIALISED = False


class _DropOrphanClientSpans(Sampler):
    """Drop a CLIENT span that has no parent, keep everything else.

    The auto-instrumented libraries trace every call they make, including the
    ones nobody asked for: arq polls Redis for its queue on a timer, and
    `/readyz` pings Postgres and Valkey on every probe. Each of those starts a
    *new trace* whose root is `ZRANGEBYSCORE` or `PING`, and in UAT they were
    27 of every 40 traces — a "latest traces" panel showing queue polling, and
    a Tempo bill for it.

    A parentless CLIENT span is infrastructure by construction: a database or
    cache call that no request, command or job asked for. The same call made
    while handling an update has the update's span as its parent and is kept,
    which is the half that matters — a command's trace still contains every
    query and every Bot API call it made.

    Kind, not name: a name list would need editing every time a library learns
    a new verb, and would silently start passing chatter through on the day it
    did.
    """

    def __init__(self, delegate: Sampler) -> None:
        self._delegate = delegate

    def should_sample(
        self,
        parent_context: Context | None,
        trace_id: int,
        name: str,
        kind: SpanKind | None = None,
        attributes: Any = None,
        links: Any = None,
        trace_state: Any = None,
    ) -> SamplingResult:
        parent = trace.get_current_span(parent_context).get_span_context()
        if kind is SpanKind.CLIENT and not parent.is_valid:
            return SamplingResult(Decision.DROP, attributes, trace_state)
        return self._delegate.should_sample(
            parent_context, trace_id, name, kind, attributes, links, trace_state
        )

    def get_description(self) -> str:
        return f"DropOrphanClientSpans({self._delegate.get_description()})"


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
        sampler=_DropOrphanClientSpans(ParentBased(TraceIdRatioBased(settings.trace_sample_ratio))),
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
    """Record the exception, and the chain of context around it.

    `record_exception` alone gives the traceback of the *outermost* failure,
    which after `errors.fail_as` is the wrapper: the frames are right and the
    message reads "ConfigWriteError: group_config.set_config(...)". The
    attributes below carry what that hides — the innermost type and message
    (the thing that actually failed) and the whole chain as one line — so a
    span list in Tempo answers "what broke" without opening a span.
    """
    from cb_core import errors  # local: cb_core.errors must not import telemetry

    sp.record_exception(exc)
    innermost = errors.root(exc) or exc
    sp.set_attribute("cb.error.type", type(innermost).__name__)
    sp.set_attribute("cb.error.message", errors.reason(exc))
    sp.set_attribute("cb.error.chain", errors.render(exc))
    sp.set_status(trace.Status(trace.StatusCode.ERROR, errors.render(exc)))
