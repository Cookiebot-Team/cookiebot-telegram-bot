"""The sampler that keeps a trace list readable.

In UAT, 27 of every 40 traces were rooted at `ZRANGEBYSCORE` — arq polling its
own queue — and most of the rest at `PING`/`SELECT` from `/readyz`. Every one is
a real span of a real call; none of them is an interaction anyone would look for,
and together they buried the command traces in the dashboard that exists to find
command traces.
"""

from __future__ import annotations

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace.sampling import ALWAYS_OFF, ALWAYS_ON, Decision, ParentBased
from opentelemetry.trace import NonRecordingSpan, SpanContext, SpanKind, TraceFlags

from cb_core.telemetry import _DropOrphanClientSpans

SAMPLER = _DropOrphanClientSpans(ParentBased(ALWAYS_ON))
TRACE_ID = 0x0F431777AB7F8F5E7DBB4B570197D700


def _parent_context() -> trace.Context:
    """A context whose current span is a real, sampled, remote-looking parent."""
    parent = NonRecordingSpan(
        SpanContext(
            trace_id=TRACE_ID,
            span_id=0xC81374DA3BCDEEE3,
            is_remote=False,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        )
    )
    return trace.set_span_in_context(parent)


def _decide(kind: SpanKind, parent: trace.Context | None) -> Decision:
    return SAMPLER.should_sample(parent, TRACE_ID, "SELECT", kind=kind).decision


class TestOrphanInfrastructureIsDropped:
    def test_a_parentless_client_span_is_dropped(self) -> None:
        """A database or cache call nobody asked for: a queue poll, a health
        probe. It is infrastructure by construction — no request, command or job
        is above it."""
        assert _decide(SpanKind.CLIENT, None) is Decision.DROP

    def test_the_same_call_under_a_parent_is_kept(self) -> None:
        """The half that matters: a command's trace still contains every query
        and every Bot API call it made."""
        assert _decide(SpanKind.CLIENT, _parent_context()) is not Decision.DROP


class TestEverythingElseIsUntouched:
    @pytest.mark.parametrize(
        "kind",
        [SpanKind.SERVER, SpanKind.CONSUMER, SpanKind.INTERNAL, SpanKind.PRODUCER],
    )
    def test_other_parentless_roots_are_kept(self, kind: SpanKind) -> None:
        """A webhook (SERVER), a worker job (CONSUMER) and an explicit `span()`
        (INTERNAL) are all legitimately parentless — they are where a trace
        starts."""
        assert _decide(kind, None) is not Decision.DROP

    def test_the_delegate_still_decides_what_it_is_given(self) -> None:
        never = _DropOrphanClientSpans(ParentBased(ALWAYS_OFF))
        assert never.should_sample(None, TRACE_ID, "x", kind=SpanKind.SERVER).decision is (
            Decision.DROP
        )

    def test_the_description_names_the_delegate(self) -> None:
        assert "DropOrphanClientSpans" in SAMPLER.get_description()
        assert ALWAYS_ON.get_description() in SAMPLER.get_description()
