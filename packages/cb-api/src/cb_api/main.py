"""cb-api — the Python replacement for COOKIEBOT-backend (Spring Boot + MongoDB).

M0 ships the service shell: lifespan-managed pool, telemetry, metrics, health.
Domain routers land in M1 as their QA scenarios come online.

Deliberate departures from the Java service (see FEATURE-MAP §6):
  D11  every list endpoint will be keyset-paginated; no unbounded findAll()
  D12  health/metrics are not anonymous-by-default here
  D13  CORS is an explicit allowlist, never "*" with credentials
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from cb_api.routers import health
from cb_core import cache, db, metrics, storage
from cb_core.logging import configure_logging, get_logger
from cb_core.migrations import ensure_schema
from cb_core.settings import get_settings
from cb_core.telemetry import setup_tracing

settings = get_settings()
settings.service_name = "cb-api"
configure_logging(settings)
setup_tracing(settings)
log = get_logger("cb.api")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await ensure_schema(settings)
    await db.init_pool(settings)
    await cache.init_cache(settings)
    await storage.init_storage(settings)
    from cb_core.cooldowns import COMPILED

    metrics.start_metrics_server(9102, "cb-api", "0.1.0", COMPILED)
    log.info("api.started", env=settings.env, cython=COMPILED)
    try:
        yield
    finally:
        await storage.close_storage()
        await cache.close_cache()
        await db.close_pool()
        log.info("api.stopped")


app = FastAPI(
    title="Cookiebot API",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.is_local else None,
    redoc_url=None,
)

# Explicit allowlist. The Java service shipped allowed-origins:"*" together with
# allow-credentials:true, which browsers reject and which is unsafe regardless.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"] if settings.is_local else [],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["authorization", "content-type"],
)

app.include_router(health.router)
FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,readyz")
