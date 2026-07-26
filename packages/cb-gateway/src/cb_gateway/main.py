"""cb-gateway — Telegram ingest.

Webhook, not long polling: v1 ran five processes each long-polling `getUpdates`
with a 50-thread pool, supervised by a script that killed the process whenever
host CPU crossed 70%. This service is stateless, so capacity is replicas.

Ingest only. Anything slow (ffmpeg, image compositing, LLM calls, fan-out to many
chats) is enqueued for cb-worker, so a `/destroy` video job can never delay a
captcha reply — v1's single global spin-lock did exactly that.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from aiogram import Dispatcher
from aiogram.types import Update
from fastapi import FastAPI, Header, HTTPException, Request, Response, status
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from cb_core import cache, db, group_config, metrics, storage
from cb_core.events import recorder
from cb_core.llm import close_llm, init_llm
from cb_core.logging import configure_logging, get_logger
from cb_core.migrations import ensure_schema
from cb_core.settings import get_settings
from cb_core.telemetry import setup_tracing
from cb_gateway.bots import registry
from cb_gateway.handlers import build_router
from cb_gateway.ingest import build_ingest
from cb_gateway.middlewares import DedupeMiddleware, TelemetryMiddleware

settings = get_settings()
settings.service_name = "cb-gateway"
configure_logging(settings)
setup_tracing(settings)
log = get_logger("cb.gateway")

dp = Dispatcher()
dp.update.outer_middleware(DedupeMiddleware())
dp.update.outer_middleware(TelemetryMiddleware())
dp.include_router(build_router())


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await ensure_schema(settings)
    await db.init_pool(settings)
    await cache.init_cache(settings)
    await storage.init_storage(settings)
    init_llm(settings)
    # Config changes made by any replica must reach this one without a restart —
    # v1 needed a manual /reload in each of its five processes and still drifted.
    await group_config.start_invalidation_listener()
    await recorder().start()
    await registry.load(settings)
    await registry.resolve_usernames()

    from cb_core.cooldowns import COMPILED

    metrics.start_metrics_server(settings.metrics_port, "cb-gateway", "0.1.0", COMPILED)

    ingest = build_ingest(registry, dp, settings)
    await ingest.start()

    log.info(
        "gateway.started",
        skins=registry.skins(),
        cython=COMPILED,
        ingest=ingest.mode,
        telegram_api="self-hosted" if settings.telegram_api_local else "cloud",
    )
    try:
        yield
    finally:
        await ingest.stop()
        await group_config.stop_invalidation_listener()
        await recorder().stop()
        await registry.close()
        await close_llm()
        await storage.close_storage()
        await cache.close_cache()
        await db.close_pool()
        log.info("gateway.stopped")


app = FastAPI(title="Cookiebot Gateway", version="0.1.0", lifespan=lifespan, docs_url=None)


@app.post("/webhook/{skin}")
async def webhook(
    skin: str,
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> Response:
    """Telegram delivers here. Non-2xx means Telegram redelivers, so we 200 on
    handler errors and surface the failure through metrics/traces instead —
    otherwise one broken handler turns into an infinite redelivery loop."""
    if settings.webhook_secret and x_telegram_bot_api_secret_token != settings.webhook_secret:
        metrics.updates_dropped_total.labels(reason="bad_secret").inc()
        raise HTTPException(status.HTTP_403_FORBIDDEN, "bad secret token")

    bot = registry.get(skin)
    if bot is None:
        metrics.updates_dropped_total.labels(reason="unknown_skin").inc()
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown skin {skin!r}")

    payload = await request.json()
    update = Update.model_validate(payload, context={"bot": bot})
    try:
        await dp.feed_update(bot, update, skin=skin, bot_username=registry.username(skin))
    except Exception:  # noqa: BLE001 - already traced and counted in middleware
        return Response(status_code=status.HTTP_200_OK)
    return Response(status_code=status.HTTP_200_OK)


@app.get("/healthz")
async def healthz() -> dict[str, object]:
    return {"status": "ok", "service": "cb-gateway", "skins": registry.skins()}


@app.get("/readyz")
async def readyz(response: Response) -> dict[str, object]:
    pg = await db.healthcheck()
    valkey = await cache.healthcheck()
    ready = pg and valkey and bool(registry.skins())
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"ready": ready, "postgres": pg, "valkey": valkey, "bots": len(registry.skins())}


FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,readyz")
