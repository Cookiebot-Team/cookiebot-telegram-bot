"""Liveness and readiness.

Split on purpose: k8s should restart on /healthz but only pull from the load
balancer on /readyz. The Java service exposed a single actuator endpoint,
unauthenticated, on the public listener.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response, status
from pydantic import BaseModel, Field

from cb_core import cache, db
from cb_core.cooldowns import COMPILED

router = APIRouter(tags=["health"])


class Liveness(BaseModel):
    """What a liveness probe gets: the process identifying itself."""

    status: str = Field(examples=["ok"])
    service: str
    version: str
    cython: bool = Field(description="whether the compiled hot path is in this build")


class Readiness(BaseModel):
    """Each dependency, separately. One flag for "should I get traffic" would
    make the two failures indistinguishable in the one place they need to be
    told apart."""

    ready: bool
    postgres: bool
    valkey: bool


@router.get(
    "/healthz",
    summary="Is the process up?",
    response_model=Liveness,
)
async def healthz() -> dict[str, Any]:
    """Process is up. No dependency checks — a DB blip must not trigger restarts."""
    return {"status": "ok", "service": "cb-api", "version": "0.1.0", "cython": COMPILED}


@router.get(
    "/readyz",
    summary="Are this process's dependencies reachable?",
    response_model=Readiness,
    responses={503: {"model": Readiness, "description": "pulled from rotation, not restarted"}},
)
async def readyz(response: Response) -> dict[str, Any]:
    """Dependencies reachable. Fails -> pulled from rotation, not restarted."""
    pg = await db.healthcheck()
    valkey = await cache.healthcheck()
    ready = pg and valkey
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"ready": ready, "postgres": pg, "valkey": valkey}
