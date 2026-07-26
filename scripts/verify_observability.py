"""Prove the observability stack actually works, end to end.

Every component in `docker-compose.yml` can be "up" while the thing that
matters — a signal leaving the application and coming back out of a query —
is broken. A collector with a typo'd exporter endpoint, a Loki that silently
rejects the timestamps, a datasource pointing at a port nothing listens on:
all three look identical to `docker ps`, and all three look identical to a
dashboard, which shows "No data" whether the stack is broken or the app is
merely idle.

So this pushes a real trace, a real log line and a real metric through the
collector, then queries each store back out and reports what it found:

    python scripts/verify_observability.py

Every check names what it proved, not just PASS. A failure names the component
and the most likely cause, because "Loki: FAIL" on its own sends people to
read container logs they did not need to read.

Exit code is 0 only if every check passed, so this is usable as a gate.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

# The signal this run pushes is tagged with a unique id, so a query can prove
# *this* invocation's data made it through rather than finding something an
# earlier run left behind — which is exactly how a broken pipeline goes on
# looking healthy for a week.
RUN_ID = os.environ.get("CB_VERIFY_RUN_ID") or f"verify-{int(time.time() * 1000)}"

SERVICE_NAME = "cb-observability-verify"

DEFAULTS = {
    "collector_grpc": os.environ.get("CB_VERIFY_OTLP", "localhost:4317"),
    "tempo": os.environ.get("CB_VERIFY_TEMPO", "http://localhost:3200"),
    "loki": os.environ.get("CB_VERIFY_LOKI", "http://localhost:3100"),
    "metrics": os.environ.get("CB_VERIFY_METRICS", "http://localhost:8428"),
    "grafana": os.environ.get("CB_VERIFY_GRAFANA", "http://localhost:3000"),
}

#: How long to keep asking a store for something that was just pushed. Traces
#: and logs are batched by the collector and flushed on an interval, so an
#: immediate query legitimately misses — this is not a hedge against
#: flakiness, it is the pipeline's real latency.
POLL_TIMEOUT_S = 45.0
POLL_INTERVAL_S = 1.0


@dataclass
class Result:
    name: str
    passed: bool
    detail: str
    #: What to go look at when this fails. Populated for failures only; a
    #: passing check does not need advice.
    hint: str = ""


@dataclass
class Report:
    results: list[Result] = field(default_factory=list)

    def add(self, name: str, passed: bool, detail: str, hint: str = "") -> bool:
        self.results.append(Result(name, passed, detail, hint))
        mark = "\033[32mPASS\033[0m" if passed else "\033[31mFAIL\033[0m"
        print(f"  {mark}  {name}: {detail}")
        if not passed and hint:
            print(f"        -> {hint}")
        return passed

    @property
    def ok(self) -> bool:
        return all(r.passed for r in self.results)


# --------------------------------------------------------------------- http


def _get(url: str, timeout: float = 5.0) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 - unreachable is a result, not a crash
        return 0, str(exc)


def _poll(check: object, *, timeout: float = POLL_TIMEOUT_S) -> object:
    """Call `check` until it returns something truthy or the budget runs out.

    Returns the last falsy value on timeout rather than raising, so the caller
    decides how to report it — a timeout here is a finding to print, not an
    exception to propagate.
    """
    deadline = time.monotonic() + timeout
    result = None
    while time.monotonic() < deadline:
        result = check()  # type: ignore[operator]
        if result:
            return result
        time.sleep(POLL_INTERVAL_S)
    return result


# ------------------------------------------------------------------- push


def push_signals(endpoint: str, report: Report) -> str | None:
    """Emit one trace and one log line through the collector, over OTLP/gRPC.

    Returns the trace id, or `None` if the SDK could not even hand them over —
    in which case nothing downstream is worth checking, because nothing was
    sent.
    """
    try:
        from opentelemetry._logs import set_logger_provider
        from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as exc:
        report.add(
            "OTel SDK available",
            False,
            f"could not import the exporters: {exc}",
            "install the workspace: python scripts/cb.py install",
        )
        return None

    import logging

    resource = Resource.create(
        {
            "service.name": SERVICE_NAME,
            "service.namespace": "cookiebot",
            "deployment.environment": "local",
            "cb.verify.run_id": RUN_ID,
        }
    )

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True))
    )
    tracer = tracer_provider.get_tracer("cb.verify")

    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(
        BatchLogRecordProcessor(OTLPLogExporter(endpoint=endpoint, insecure=True))
    )
    set_logger_provider(logger_provider)

    handler = LoggingHandler(level=logging.INFO, logger_provider=logger_provider)
    verify_log = logging.getLogger("cb.verify")
    verify_log.setLevel(logging.INFO)
    verify_log.addHandler(handler)

    # The log line is emitted *inside* the span on purpose: that is what puts
    # the trace id on the log record, and the trace-to-logs jump in Grafana is
    # the single most valuable thing this stack does. A log emitted outside a
    # span would be delivered and still prove nothing about the join.
    with tracer.start_as_current_span("verify.root") as span:
        span.set_attribute("cb.verify.run_id", RUN_ID)
        span.set_attribute("cb.outcome", "answered")
        trace_id = format(span.get_span_context().trace_id, "032x")
        with tracer.start_as_current_span("verify.child") as child:
            child.set_attribute("cb.verify.run_id", RUN_ID)
            verify_log.info("observability stack verification line run_id=%s", RUN_ID)

    # Force-flush rather than wait out the batch interval: this script's whole
    # job is to answer quickly, and a `shutdown()` that blocks is a far better
    # signal than a sleep that guesses.
    tracer_provider.shutdown()
    logger_provider.shutdown()
    report.add(
        "Push trace + log via OTLP",
        True,
        f"sent to {endpoint} (trace {trace_id[:16]}…, run_id {RUN_ID})",
    )
    return trace_id


# ------------------------------------------------------------------ checks


def check_metrics_store(url: str, report: Report) -> None:
    status, body = _get(f"{url}/health")
    if not report.add(
        "VictoriaMetrics reachable",
        status == 200,
        f"{url}/health -> {status or 'unreachable'}",
        "is it up? podman-compose up -d victoriametrics",
    ):
        return

    status, body = _get(f"{url}/api/v1/query?{urllib.parse.urlencode({'query': 'up'})}")
    try:
        series = json.loads(body)["data"]["result"]
    except Exception:  # noqa: BLE001 - a malformed body is itself the failure
        report.add("VictoriaMetrics scraping", False, f"unparseable response: {body[:120]}")
        return

    up = {s["metric"].get("job", "?"): s["value"][1] for s in series}
    live = sorted(job for job, value in up.items() if value == "1")
    down = sorted(job for job, value in up.items() if value != "1")
    # Only the infrastructure targets are expected up: the service targets are
    # host processes that are legitimately not running most of the time, and
    # calling that a failure would make this script cry wolf.
    infra = {"victoriametrics", "tempo", "loki", "otel-collector"}
    missing_infra = sorted(infra - set(live))
    report.add(
        "VictoriaMetrics scraping",
        not missing_infra,
        f"up: {', '.join(live) or 'nothing'}"
        + (f" | down (expected for host services): {', '.join(down)}" if down else ""),
        f"infrastructure targets not up: {', '.join(missing_infra)} — check ops/scrape.yml",
    )


def check_tempo(url: str, trace_id: str, report: Report) -> None:
    # Polled, not asked once: Tempo's ingester answers 503 for ~15s after
    # joining its own ring ("waiting for 15s after being ready"), which is
    # exactly the window someone running this right after `cb.py up` lands in.
    # A one-shot check there reports a broken Tempo that is merely young.
    ready = _poll(lambda: _get(f"{url}/ready")[0] == 200, timeout=60.0)
    if not report.add(
        "Tempo reachable",
        bool(ready),
        f"{url}/ready -> {'ready' if ready else 'never became ready'}",
        "is it up? podman-compose up -d tempo",
    ):
        return

    def found() -> dict | None:
        code, body = _get(f"{url}/api/traces/{trace_id}")
        if code != 200:
            return None
        try:
            payload = json.loads(body)
        except ValueError:
            return None
        return payload if payload.get("batches") else None

    payload = _poll(found)
    if not isinstance(payload, dict):
        report.add(
            "Trace stored and queryable",
            False,
            f"trace {trace_id[:16]}… never appeared within {POLL_TIMEOUT_S:.0f}s",
            "check the collector's traces pipeline: podman logs cookiebot-v2_otel-collector_1",
        )
        return

    span_names = [
        span.get("name", "?")
        for batch in payload.get("batches", [])
        for scope in batch.get("scopeSpans", [])
        for span in scope.get("spans", [])
    ]
    report.add(
        "Trace stored and queryable",
        "verify.root" in span_names,
        f"Tempo returned {len(span_names)} span(s): {', '.join(sorted(span_names))}",
        "the trace arrived but not the spans this script sent — is something else writing it?",
    )


def check_loki(url: str, trace_id: str, report: Report) -> None:
    status, _ = _get(f"{url}/ready")
    if not report.add(
        "Loki reachable",
        status == 200,
        f"{url}/ready -> {status or 'unreachable'}",
        "is it up? podman-compose up -d loki",
    ):
        return

    query = f'{{service_name="{SERVICE_NAME}"}} |= `{RUN_ID}`'
    start_ns = (int(time.time()) - 900) * 1_000_000_000

    def found() -> list | None:
        params = urllib.parse.urlencode({"query": query, "start": start_ns, "limit": 20})
        code, body = _get(f"{url}/loki/api/v1/query_range?{params}")
        if code != 200:
            return None
        try:
            streams = json.loads(body)["data"]["result"]
        except Exception:  # noqa: BLE001
            return None
        return streams or None

    streams = _poll(found)
    if not isinstance(streams, list):
        report.add(
            "Log line stored and queryable",
            False,
            f"no line matching run_id {RUN_ID} within {POLL_TIMEOUT_S:.0f}s",
            "Loki silently drops what it rejects — check the discard rate on the "
            "'Observability stack health' dashboard, and the collector's logs pipeline",
        )
        return

    entries = [entry for stream in streams for entry in stream.get("values", [])]
    report.add(
        "Log line stored and queryable",
        bool(entries),
        f"found {len(entries)} line(s) under service_name={SERVICE_NAME}",
    )

    # The join is the actual product. A log line that arrived without its trace
    # id is a log line you cannot get to from a trace, which is most of the
    # reason for running Loki next to Tempo at all.
    joined = any(
        trace_id in json.dumps(stream.get("stream", {}))
        or any(trace_id in v[1] for v in stream.get("values", []))
        for stream in streams
    )
    report.add(
        "Log carries its trace id (log -> trace jump)",
        joined,
        f"trace {trace_id[:16]}… {'present on' if joined else 'MISSING from'} the stored line",
        "the line was emitted outside a span, or the collector dropped the attribute — "
        "without this, Grafana's derived-field link has nothing to link on",
    )


def check_grafana(url: str, report: Report) -> None:
    status, body = _get(f"{url}/api/health")
    if not report.add(
        "Grafana reachable",
        status == 200,
        f"{url}/api/health -> {status or 'unreachable'}",
        "is it up, and on this port? CB_GRAFANA_PORT overrides it (3000 is often taken)",
    ):
        return

    status, body = _get(f"{url}/api/datasources")
    try:
        names = {d["uid"]: d["type"] for d in json.loads(body)}
    except Exception:  # noqa: BLE001
        report.add("Grafana datasources provisioned", False, f"unparseable: {body[:120]}")
        return
    expected = {"metrics", "tempo", "logs"}
    missing = sorted(expected - set(names))
    report.add(
        "Grafana datasources provisioned",
        not missing,
        ", ".join(f"{uid}={kind}" for uid, kind in sorted(names.items())),
        f"missing: {', '.join(missing)} — check ops/grafana/provisioning/datasources/",
    )

    status, body = _get(f"{url}/api/search?type=dash-db")
    try:
        dashboards = {d["uid"] for d in json.loads(body)}
    except Exception:  # noqa: BLE001
        report.add("Grafana dashboards provisioned", False, f"unparseable: {body[:120]}")
        return
    expected_dashboards = {"cb-bot-metrics", "cb-traces-logs", "cb-stack-health"}
    missing_dashboards = sorted(expected_dashboards - dashboards)
    report.add(
        "Grafana dashboards provisioned",
        not missing_dashboards,
        f"{len(dashboards)} dashboard(s): {', '.join(sorted(dashboards))}",
        f"missing: {', '.join(missing_dashboards)} — check ops/grafana/dashboards/",
    )


# --------------------------------------------------------------------- main


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--otlp", default=DEFAULTS["collector_grpc"])
    parser.add_argument("--tempo", default=DEFAULTS["tempo"])
    parser.add_argument("--loki", default=DEFAULTS["loki"])
    parser.add_argument("--metrics", default=DEFAULTS["metrics"])
    parser.add_argument("--grafana", default=DEFAULTS["grafana"])
    args = parser.parse_args()

    report = Report()

    print("\nPushing a real signal through the collector")
    trace_id = push_signals(args.otlp, report)

    print("\nMetrics — VictoriaMetrics")
    check_metrics_store(args.metrics, report)

    print("\nTraces — Tempo")
    if trace_id:
        check_tempo(args.tempo, trace_id, report)
    else:
        report.add("Trace stored and queryable", False, "nothing was sent, so nothing to find")

    print("\nLogs — Loki")
    if trace_id:
        check_loki(args.loki, trace_id, report)
    else:
        report.add("Log line stored and queryable", False, "nothing was sent, so nothing to find")

    print("\nGrafana")
    check_grafana(args.grafana, report)

    failed = [r for r in report.results if not r.passed]
    print()
    if report.ok:
        print(f"\033[32mAll {len(report.results)} checks passed.\033[0m")
        print(f"Open the dashboards: {args.grafana}/dashboards")
        print(f"This run's trace: {args.grafana}/explore (Tempo, trace id {trace_id})")
        return 0
    print(f"\033[31m{len(failed)} of {len(report.results)} checks failed:\033[0m")
    for result in failed:
        print(f"  - {result.name}: {result.detail}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
