"""cb-sandbox — a local Telegram any bot can talk to.

Two surfaces on one port:

    /bot<token>/<method>   Telegram's Bot API, as a bot's HTTP client expects it
    /api/...               the control plane the web client and the test kit drive

Point the bot at it and nothing about the bot changes — with aiogram, that is
its API base plus polling ingest; every other library has an equivalent pair:

    TELEGRAM_API_BASE=http://localhost:8083
    (long polling, not webhooks)

The bot long-polls `getUpdates` here exactly as it would poll Telegram, so what
the web client exercises is the production handler stack — the same routers,
middlewares, database and cache — not a re-implementation of it.

What makes this *your* bot's sandbox is `sandbox.config.json`: identity, seeds,
features and commands, all data. See `config.py`.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from cb_sandbox.config import get_config
from cb_sandbox.control_api import router as control_router
from cb_sandbox.logging import get_logger
from cb_sandbox.state import store
from cb_sandbox.telegram_api import router as telegram_router
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

log = get_logger("cb.sandbox")

#: Extra browser origins allowed to drive `/api/...`, comma-separated. The web
#: client's own dev server is allowed by default; anything else (a client
#: served from a different port, a second workbench) has to say so.
CORS_ORIGINS_ENV = "CB_SANDBOX_CORS_ORIGINS"
_DEFAULT_CORS_ORIGINS = ("http://localhost:3001", "http://127.0.0.1:3001")


def _cors_origins() -> list[str]:
    raw = os.environ.get(CORS_ORIGINS_ENV, "")
    extra = [origin.strip() for origin in raw.split(",") if origin.strip()]
    return [*_DEFAULT_CORS_ORIGINS, *extra]


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Leave a readable recording behind.

    A sandbox process almost always ends by SIGTERM — Ctrl-C in a terminal, a
    test session's teardown — which closes no DuckDB connection, so whatever
    is still in the write-ahead log stays there. That matters because the run
    *is* the artefact: a test run keeps its file specifically so the web UI
    can open it afterwards, and a file whose WAL never got folded in is one
    more thing between a person and the answer they came for.
    """
    yield
    store().db.checkpoint()


app = FastAPI(title="Telegram Bot Sandbox", version="0.1.0", docs_url="/docs", lifespan=lifespan)

# The web client runs on another port in development. This service is a local
# workbench that must never be exposed, so a permissive origin list here is not
# the same decision a public API would make.
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(telegram_router)
app.include_router(control_router)


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    """Readiness, plus which bot and which config this process came up with —
    the two facts that explain almost every "the sandbox is running but the
    bot does nothing" report, and the ones a launcher can assert on."""
    config = get_config()
    return {
        "status": "ok",
        "service": "cb-sandbox",
        "bot": {"id": config.bot.id, "username": config.bot.username},
        "config": config.source_path or "built-in defaults",
    }
