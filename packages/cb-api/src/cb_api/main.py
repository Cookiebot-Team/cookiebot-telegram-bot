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

from cb_api.routers import analytics, groups, health, login, oauth
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

    metrics.start_metrics_server(settings.metrics_port, "cb-api", "0.1.0", COMPILED)
    log.info("api.started", env=settings.env, cython=COMPILED)
    try:
        yield
    finally:
        await storage.close_storage()
        await cache.close_cache()
        await db.close_pool()
        log.info("api.stopped")


# The tags the Mini App's generated client is built from. `/openapi.json` is
# served everywhere (the Swagger *page* is local-only, D12's spirit: a UI that
# executes requests is not something a production deployment hands out), so a
# client generator can point at any environment and get the shapes.
_TAGS = [
    {
        "name": "auth",
        "description": (
            "The OAuth2 token endpoint. A Mini App posts Telegram's `initData`, "
            "the web console posts the login widget's payload, both refresh. "
            "Discover it from `/.well-known/openid-configuration`."
        ),
    },
    {
        "name": "groups",
        "description": (
            "Everything `/config` does in a Telegram chat: settings, rules, the "
            "welcome message, and the audit trail. Every path carries the group, "
            "and only its admins (and the tenant's owners) get an answer."
        ),
    },
    {"name": "analytics", "description": "Per-group rollups, keyset-paginated."},
]

app = FastAPI(
    title="Cookiebot API",
    version="0.1.0",
    summary="The HTTP surface behind the Telegram Mini App and the web console.",
    lifespan=lifespan,
    openapi_tags=_TAGS,
    docs_url="/docs" if settings.is_local else None,
    redoc_url=None,
)

# Explicit allowlist. The Java service shipped allowed-origins:"*" together with
# allow-credentials:true, which browsers reject and which is unsafe regardless.
# v1's own Flask app did the same for `/login` alone (`Server.py:22`), so
# `CB_WEBHUB_ALLOWED_ORIGINS` is where the web console's origin goes — an
# allowlist, never "*".
_origins = list(settings.webhub_allowed_origins)
if settings.is_local and "http://localhost:3000" not in _origins:
    _origins.append("http://localhost:3000")
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["authorization", "content-type"],
)

app.include_router(health.router)
# x_webhub_login: `/`, `/login` and the two `.well-known` documents.
app.include_router(login.router)
# x_analytics_api: per-group rollup reads, behind the token `/login` mints and
# `group_admins` membership (cb_api/security.py).
app.include_router(analytics.router)
# x_miniapp_auth: the OAuth2 token endpoint the Mini App exchanges Telegram's
# `initData` at, plus refresh and revocation. Same signing keys and same JWKS
# as `/login`, so one resource server verifies both.
app.include_router(oauth.router)
# x_group_config_api / x_audit_log: the settings a group's admins may read and
# change over HTTP, and the trail of who changed what.
app.include_router(groups.router)
FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,readyz")
