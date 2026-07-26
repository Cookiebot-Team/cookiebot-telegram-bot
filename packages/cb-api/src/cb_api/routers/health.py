"""Liveness and readiness.

Split on purpose: k8s should restart on /healthz but only pull from the load
balancer on /readyz. The Java service exposed a single actuator endpoint,
unauthenticated, on the public listener.
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from cb_core import cache, db
from cb_core.cooldowns import COMPILED

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, object]:
    """Process is up. No dependency checks — a DB blip must not trigger restarts."""
    return {"status": "ok", "service": "cb-api", "version": "0.1.0", "cython": COMPILED}


@router.get("/readyz")
async def readyz(response: Response) -> dict[str, object]:
    """Dependencies reachable. Fails -> pulled from rotation, not restarted."""
    pg = await db.healthcheck()
    valkey = await cache.healthcheck()
    ready = pg and valkey
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"ready": ready, "postgres": pg, "valkey": valkey}
