"""Valkey client: shared cache, cooldown counters, and cache invalidation.

Replaces v1's per-process unlocked dicts (FEATURE-MAP D6). Five bot processes
each held their own `cache_configurations`/`cache_admins` with no TTL, so a
config change needed a manual `/reload` in every process and they still drifted.
Here state is shared, TTL'd, and invalidation is a pub/sub message.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, cast

import msgspec
import redis.asyncio as redis

from cb_core.logging import get_logger
from cb_core.settings import Settings

log = get_logger("cb.cache")

INVALIDATION_CHANNEL = "cb:invalidate"

_client: redis.Redis | None = None
_encoder = msgspec.msgpack.Encoder()
_decoder = msgspec.msgpack.Decoder()


async def init_cache(settings: Settings) -> redis.Redis:
    global _client
    if _client is not None:
        return _client
    _client = redis.from_url(
        settings.redis_dsn,
        decode_responses=False,
        health_check_interval=30,
        socket_keepalive=True,
    )
    await _client.ping()
    log.info("cache.ready")
    return _client


async def close_cache() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def client() -> redis.Redis:
    if _client is None:
        raise RuntimeError("cache not initialised; call init_cache() during startup")
    return _client


async def get_json(key: str) -> Any | None:
    # `Any`: decoded msgpack payload - shape is caller-defined.
    raw = await client().get(key)
    return _decoder.decode(raw) if raw else None


async def set_json(key: str, value: Any, ttl_seconds: int) -> None:
    # `Any`: arbitrary msgpack-encodable payload.

    await client().set(key, _encoder.encode(value), ex=ttl_seconds)


async def delete(*keys: str) -> None:
    if keys:
        await client().delete(*keys)


async def publish_invalidation(key: str) -> None:
    """Tell every replica to drop its local L1 copy of `key`."""
    await client().publish(INVALIDATION_CHANNEL, key.encode())


async def subscribe_invalidations(
    on_key: Callable[[str], None],
) -> tuple[asyncio.Task[None], redis.client.PubSub]:
    """Run `on_key` for every invalidation another replica publishes.

    Returns the task and the subscription so the caller can stop both; the task
    never raises, because a dropped pub/sub connection must degrade to TTL-only
    caching rather than take the process down.
    """
    pubsub = client().pubsub()
    await pubsub.subscribe(INVALIDATION_CHANNEL)

    async def _pump() -> None:
        try:
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                raw = message.get("data") or b""
                on_key(raw.decode() if isinstance(raw, bytes) else str(raw))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - degrade to TTL, never crash the service
            log.warning("cache.invalidation.stopped", error=str(exc))

    return asyncio.create_task(_pump(), name="cache-invalidations"), pubsub


async def healthcheck() -> bool:
    try:
        return bool(await client().ping())
    except Exception as exc:  # noqa: BLE001
        log.warning("cache.healthcheck.failed", error=str(exc))
        return False


async def incr_window(key: str, window_seconds: int) -> int:
    """Atomic fixed-window counter — sticker-spam and per-user quotas.

    v1 did check-then-act on a plain dict (`Cooldowns.py:24-47`), which races under
    its own 50-thread pool and is simply wrong across 5 processes.
    """
    async with client().pipeline(transaction=True) as pipe:
        pipe.incr(key)
        pipe.expire(key, window_seconds, nx=True)
        count, _ = await pipe.execute()
    return int(count)


# Seed-then-clamp-then-refresh has to be one round trip: a GET/compute/SET done
# from Python would race the same way v1's dict did (`Cooldowns.py:24-36`), just
# against other gateway replicas instead of other threads. EVAL is Valkey's only
# way to make "read, seed if absent, clamp, write, re-arm the TTL" atomic.
_BUMP_CLAMPED_SCRIPT = """
local current = redis.call('GET', KEYS[1])
if current == false then
    current = tonumber(ARGV[3])
else
    current = tonumber(current)
end

local value = current + tonumber(ARGV[1])
local lo = tonumber(ARGV[4])
local hi = tonumber(ARGV[5])
if value < lo then
    value = lo
elseif value > hi then
    value = hi
end

redis.call('SET', KEYS[1], value, 'EX', ARGV[2])
return value
"""


async def bump_clamped(
    key: str, delta: int, *, lo: int, hi: int, initial: int, ttl_seconds: int
) -> int | None:
    """Atomic signed counter, clamped into `[lo, hi]` — v1's per-user AI streak
    (`Cooldowns.py:5,24-36`), which `incr_window` cannot serve: that primitive
    only increments and has no way to seed a missing key or fold the result
    back into a bound.

    `None` on any Valkey error, the same swallow-and-fail-open contract as
    `incr_window`'s callers use (see `cb_gateway/handlers/stickerspam.py::_bump`)
    — except here the swallow lives in the primitive itself, because this
    counter has exactly one caller and no reason to duplicate the same
    try/except at every call site.
    """
    try:
        # redis-py's `eval()` is typed `Union[Awaitable[str], str]` even on the
        # async client (the sync signature is inherited verbatim) — the cast
        # matches what actually comes back from an async connection.
        raw = await cast(
            Awaitable[Any],
            client().eval(
                _BUMP_CLAMPED_SCRIPT,
                1,
                key,
                str(delta),
                str(ttl_seconds),
                str(initial),
                str(lo),
                str(hi),
            ),
        )
    except Exception as exc:  # noqa: BLE001 - infra outage must fail open, not raise
        log.warning("cache.bump_clamped.failed", key=key, error=str(exc))
        return None
    return int(raw)
