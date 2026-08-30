"""Prometheus metrics.

Label discipline: never label by group_id or user_id — v1 has ~1275 groups and
that would be a cardinality bomb. Per-group numbers come from the `message_events`
table in Citus, not from Prometheus.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from contextlib import contextmanager

from prometheus_client import (
    REGISTRY,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    multiprocess,
    start_http_server,
)
from prometheus_client.registry import Collector

# `PROMETHEUS_MULTIPROC_DIR` names a directory the client writes one mmap file
# per metric into, and it must exist *before the first metric is declared* — a
# `Gauge(..., multiprocess_mode=...)` below opens its file in its constructor,
# at import time. `start_metrics_server` also creates it, which is far too late:
# with the variable set (as `.env.example` sets it) and the directory absent,
# importing this module raises `FileNotFoundError` and every service dies during
# startup. A fresh clone that copied `.env.example` hit exactly that.
_MULTIPROC_DIR = os.environ.get("PROMETHEUS_MULTIPROC_DIR")
if _MULTIPROC_DIR:
    os.makedirs(_MULTIPROC_DIR, exist_ok=True)

# Latency buckets tuned for a chat bot: the interesting region is 5ms-2s.
_LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

# ---- telegram ingest ----
updates_total = Counter("cb_updates_total", "Telegram updates received", ["bot", "update_type"])
updates_dropped_total = Counter(
    "cb_updates_dropped_total", "Updates dropped before handling", ["reason"]
)
handler_duration = Histogram(
    "cb_handler_duration_seconds",
    "Handler wall time",
    ["handler", "command", "outcome"],
    buckets=_LATENCY_BUCKETS,
)
handler_errors_total = Counter(
    "cb_handler_errors_total", "Handler exceptions", ["handler", "exc_type"]
)

# ---- outbound telegram api ----
telegram_api_duration = Histogram(
    "cb_telegram_api_duration_seconds",
    "Bot API call wall time",
    ["method", "outcome"],
    buckets=_LATENCY_BUCKETS,
)
telegram_rate_limited_total = Counter(
    "cb_telegram_rate_limited_total", "429 responses from Telegram", ["method"]
)

# ---- queue / worker ----
queue_depth = Gauge("cb_queue_depth", "Pending jobs", ["queue"], multiprocess_mode="livemax")
job_duration = Histogram(
    "cb_job_duration_seconds", "Worker job wall time", ["job", "outcome"], buckets=_LATENCY_BUCKETS
)

# ---- database ----
db_pool_in_use = Gauge(
    "cb_db_pool_in_use", "Checked-out pg connections", multiprocess_mode="livesum"
)
db_pool_size = Gauge("cb_db_pool_size", "Total pg connections", multiprocess_mode="livesum")
db_query_duration = Histogram(
    "cb_db_query_duration_seconds", "Query wall time", ["stmt"], buckets=_LATENCY_BUCKETS
)

# ---- group config / admin caches ----
# Hit/miss by layer, not by group: v1's five processes each cached configs in an
# unlocked dict with no TTL and no invalidation (FEATURE-MAP D6), and nobody could
# tell a stale read from a fresh one. These make the layers observable.
cache_lookups_total = Counter(
    "cb_cache_lookups_total", "Read-through cache lookups", ["cache", "layer", "outcome"]
)
cache_invalidations_total = Counter(
    "cb_cache_invalidations_total", "Invalidation messages", ["cache", "direction"]
)
config_fallback_total = Counter(
    "cb_config_fallback_total", "Config reads served from defaults after a failure", ["reason"]
)

# ---- external deps (cas.chat, saucenao, shazam, openai...) ----
external_dep_up = Gauge("cb_external_dep_up", "1 = last call succeeded", ["dep"])
external_dep_duration = Histogram(
    "cb_external_dep_duration_seconds",
    "External call wall time",
    ["dep", "outcome"],
    buckets=_LATENCY_BUCKETS,
)

# ---- blob storage ----
storage_duration = Histogram(
    "cb_storage_duration_seconds",
    "Blob storage call wall time",
    ["backend", "operation", "outcome"],
    buckets=_LATENCY_BUCKETS,
)
storage_errors_total = Counter(
    "cb_storage_errors_total", "Blob storage failures", ["backend", "operation"]
)
storage_bytes_total = Counter(
    "cb_storage_bytes_total", "Bytes written to blob storage", ["backend", "kind"]
)
media_dedupe_total = Counter(
    "cb_media_dedupe_total", "Media uploads resolved by content hash", ["kind", "result"]
)

# ---- llm (v1 had zero visibility into token spend) ----
llm_tokens_total = Counter("cb_llm_tokens_total", "LLM tokens", ["provider", "model", "kind"])
llm_cost_usd_total = Counter("cb_llm_cost_usd_total", "Estimated LLM spend", ["provider", "model"])
llm_duration = Histogram(
    "cb_llm_duration_seconds",
    "LLM call wall time",
    ["provider", "model", "task", "outcome"],
    buckets=(0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 20.0, 45.0, 90.0),
)
llm_requests_total = Counter(
    "cb_llm_requests_total", "LLM calls", ["provider", "model", "task", "outcome"]
)
llm_refusals_total = Counter(
    "cb_llm_refusals_total", "Provider safety refusals", ["provider", "model", "category"]
)
llm_budget_check_failed_total = Counter(
    "cb_llm_budget_check_failed_total",
    "Tenant budget check failed open on a cache or database error (R2.4)",
)

# ---- audit trail (x_audit_log) ----
# Labelled with the action, never with the group or the actor: a per-group
# label on a metric is the cardinality bomb AGENTS.md §7 forbids, and per-group
# counts are a Citus query over `group_audit_events` anyway.
audit_events_total = Counter("cb_audit_events_total", "Audit rows written", ["action", "surface"])
audit_write_failures_total = Counter(
    "cb_audit_write_failures_total",
    "Audit rows that could not be written after the action they describe succeeded",
    ["action"],
)

# ---- Mini App sessions (x_miniapp_auth) ----
auth_tokens_issued_total = Counter("cb_auth_tokens_issued_total", "Access tokens minted", ["grant"])
auth_grants_rejected_total = Counter(
    "cb_auth_grants_rejected_total", "Token requests refused", ["grant", "reason"]
)
auth_refresh_reuse_total = Counter(
    "cb_auth_refresh_reuse_total",
    "Refresh tokens presented after rotation — the whole family is revoked",
)

# ---- build info ----
build_info = Gauge("cb_build_info", "Build metadata", ["service", "version", "cython"])


def start_metrics_server(port: int, service: str, version: str, cython_compiled: bool) -> None:
    """Expose /metrics. Under granian with >1 worker, PROMETHEUS_MULTIPROC_DIR must be set.

    Falls back to the default process-wide registry rather than `None`: passing
    `registry=None` is accepted by `start_http_server` and then fails on every
    scrape with `AttributeError: 'NoneType' object has no attribute 'collect'`,
    which the server turns into a 500. Nothing would have noticed except an
    empty dashboard — the process starts fine and the failure only appears when
    Prometheus asks.
    """
    registry: Collector = REGISTRY
    mp_dir = os.environ.get("PROMETHEUS_MULTIPROC_DIR")
    if mp_dir:
        # Import time already created it (see `_MULTIPROC_DIR`); repeated here
        # for the case where the variable was set after this module loaded.
        os.makedirs(mp_dir, exist_ok=True)
        multiproc_registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(multiproc_registry)
        registry = multiproc_registry

    build_info.labels(service=service, version=version, cython=str(cython_compiled).lower()).set(1)
    start_http_server(port, registry=registry)


@contextmanager
def timed(histogram: Histogram, **labels: str) -> Iterator[None]:
    """Time a block, tagging outcome=ok|error automatically."""
    start = time.perf_counter()
    outcome = "ok"
    try:
        yield
    except Exception:
        outcome = "error"
        raise
    finally:
        if "outcome" in histogram._labelnames:  # noqa: SLF001
            histogram.labels(**labels, outcome=outcome).observe(time.perf_counter() - start)
        else:
            histogram.labels(**labels).observe(time.perf_counter() - start)
