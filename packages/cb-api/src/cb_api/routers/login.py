"""x_webhub_login — the four endpoints `COOKIEBOT-WebHub` talks to.

v1: `../COOKIEBOT-Telegram-Group-Bot/Bot/Server.py` — a Flask app on `:8080`.
Contract: `docs/contracts/x_webhub_login.md`.

The response *shapes* are v1's, because `../COOKIEBOT-WebHub/src/lib/api/axios.ts`
reads them by name: `accessToken` from `/login`, and `exp` out of the token
itself through `jwt-decode`. Everything behind them is different, and the
differences are in the contract.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response, status

from cb_api import auth, keys
from cb_core import ops
from cb_core.logging import get_logger
from cb_core.settings import get_settings

log = get_logger("cb.api.login")

router = APIRouter(tags=["webhub"])


def _issuer(request: Request) -> str:
    """`CB_WEBHUB_ISSUER`, or v1's `request.url_root.rstrip('/')`.

    v1 had no setting, so its issuer was whatever `Host`/`X-Forwarded-Host`
    said — behind its own `ProxyFix(x_host=1)`, that is caller-controlled. The
    fallback is kept so an unconfigured deployment behaves exactly as v1 did;
    setting the value is what closes it, and there is no value v2 could guess.
    """
    configured = get_settings().webhub_issuer
    return configured or str(request.base_url).rstrip("/")


@router.get("/")
async def home() -> dict[str, Any]:
    """v1 `Server.py:55-57`. `number_chats` was the module constant
    `NUMBER_CHATS = 1275` (`:17`) that nothing ever updated; here it is the
    real count."""
    return {"status": "Bot is online", "number_chats": await ops.count_groups()}


@router.post("/login")
async def login(payload: dict[str, Any], request: Request, response: Response) -> dict[str, Any]:
    """Exchange a Telegram login-widget payload for a JWT.

    v1 `Server.py:59-76`. The status codes and the two error bodies are v1's,
    because the client branches on them.
    """
    settings = get_settings()
    if not payload:
        response.status_code = status.HTTP_400_BAD_REQUEST
        return {"error": "Missing data"}

    # v1 tried five hardcoded env vars in order (`:62-68`) — and could only
    # ever match the first, see D-WL-2. Every configured skin's token is a
    # valid signer here, which is what that loop was written to mean.
    tokens = tuple(get_settings().bot_tokens.values())
    if not any(auth.validate_telegram_auth(payload, token) for token in tokens):
        response.status_code = status.HTTP_401_UNAUTHORIZED
        return {"error": "Invalid bot token"}

    if not auth.is_fresh(payload, settings.webhub_auth_max_age_seconds):
        # Off by default (the setting reproduces v1, which never looked); when
        # it is on, a replayed payload is indistinguishable from an expired
        # login and gets the same answer as a bad signature.
        log.info("login.stale_auth_date")
        response.status_code = status.HTTP_401_UNAUTHORIZED
        return {"error": "Invalid bot token"}

    key = await keys.signing_key()
    claims = auth.build_claims(
        subject=str(payload.get("id", "")),
        issuer=_issuer(request),
        kid=key.kid,
        ttl_seconds=settings.webhub_token_ttl_seconds,
    )
    return {
        "status": "Token generated",
        "accessToken": auth.issue_token(claims, key.private_pem, key.kid),
    }


@router.get("/.well-known/jwks.json")
async def jwks() -> dict[str, Any]:
    """v1 `Server.py:78-84`. v1 published the key of whichever gunicorn worker
    answered; this publishes every key the deployment might have signed with,
    so a rotation can overlap."""
    published = await keys.published_keys()
    return {"keys": [keys.public_jwk(k.private_pem, k.kid) for k in published]}


@router.get("/.well-known/openid-configuration")
async def openid_configuration(request: Request) -> dict[str, Any]:
    """v1 `Server.py:86-95`, verbatim."""
    base_url = _issuer(request)
    return {
        "issuer": base_url,
        "jwks_uri": f"{base_url}/.well-known/jwks.json",
        "response_types_supported": ["id_token"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["RS256"],
    }


__all__ = ["router"]
