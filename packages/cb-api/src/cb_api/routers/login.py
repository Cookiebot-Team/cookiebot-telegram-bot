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
from pydantic import BaseModel, Field

from cb_api import auth, keys
from cb_api.routers import oauth
from cb_core import ops
from cb_core.logging import get_logger
from cb_core.settings import get_settings

log = get_logger("cb.api.login")

router = APIRouter(tags=["webhub"])


class ServiceBanner(BaseModel):
    """v1's `/` — what `COOKIEBOT-WebHub` polls to decide the bot is alive."""

    status: str = Field(examples=["Bot is online"])
    number_chats: int = Field(
        description="the real count; v1 returned a module constant nothing updated"
    )


class LoginToken(BaseModel):
    """v1's success body, key for key. `COOKIEBOT-WebHub` reads `accessToken`
    by name (`src/lib/api/axios.ts`), so neither name may move."""

    status: str = Field(examples=["Token generated"])
    accessToken: str = Field(  # noqa: N815 - v1's key, and the console reads it by name
        description="RS256, verifiable against `/.well-known/jwks.json`; carries no "
        "`scope` claim, which `cb_api.security` reads as read-only"
    )


class LoginError(BaseModel):
    """v1's error body. Not FastAPI's `{"detail": ...}`: the console branches on
    `error`, and changing the key would be the one break this codebase does not
    allow."""

    error: str = Field(examples=["Invalid bot token"])


class JwksDocument(BaseModel):
    """Every key the deployment might have signed with, so a rotation overlaps."""

    keys: list[dict[str, Any]] = Field(description="public JWKs, `kid` and all")


class OpenIDConfiguration(BaseModel):
    """What a Mini App or an OAuth client library discovers this deployment
    with. The first five keys are v1's, unchanged; the rest arrived with
    `x_miniapp_auth`."""

    issuer: str
    jwks_uri: str
    response_types_supported: list[str]
    subject_types_supported: list[str]
    id_token_signing_alg_values_supported: list[str]
    token_endpoint: str
    revocation_endpoint: str
    token_endpoint_auth_methods_supported: list[str]
    grant_types_supported: list[str]
    scopes_supported: list[str]


def _issuer(request: Request) -> str:
    """`CB_WEBHUB_ISSUER`, or v1's `request.url_root.rstrip('/')`.

    v1 had no setting, so its issuer was whatever `Host`/`X-Forwarded-Host`
    said — behind its own `ProxyFix(x_host=1)`, that is caller-controlled. The
    fallback is kept so an unconfigured deployment behaves exactly as v1 did;
    setting the value is what closes it, and there is no value v2 could guess.
    """
    configured = get_settings().webhub_issuer
    return configured or str(request.base_url).rstrip("/")


# Documented through `responses` rather than `response_model` — the same reason
# the two `.well-known` documents below are: these are v1's shapes, and a model
# that filtered an unlisted key back out would turn "additive" into "silently
# truncated" the day one is added. The document gains the shape; the bytes on
# the wire are untouched.
@router.get(
    "/",
    summary="Is the bot online, and in how many groups?",
    responses={200: {"model": ServiceBanner}},
    response_model=None,
)
async def home() -> dict[str, Any]:
    """v1 `Server.py:55-57`. `number_chats` was the module constant
    `NUMBER_CHATS = 1275` (`:17`) that nothing ever updated; here it is the
    real count."""
    return {"status": "Bot is online", "number_chats": await ops.count_groups()}


@router.post(
    "/login",
    summary="Exchange a Telegram login-widget payload for a JWT (v1's endpoint)",
    responses={
        200: {"model": LoginToken, "description": "v1's body, key for key"},
        400: {"model": LoginError, "description": "an empty payload — v1's `Missing data`"},
        401: {
            "model": LoginError,
            "description": "the signature did not verify, or the payload is stale where "
            "`CB_WEBHUB_AUTH_MAX_AGE_SECONDS` is set — one answer for both, so a "
            "forgery cannot learn which of the two it got closest to",
        },
    },
    response_model=None,
)
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


# Documented through `responses` rather than `response_model`: these two are
# v1's documents, and a model that filtered an unlisted key back out would turn
# "additive" into "silently truncated" the day one is added.
@router.get(
    "/.well-known/jwks.json",
    summary="The signing keys every token here verifies against",
    responses={200: {"model": JwksDocument}},
    response_model=None,
)
async def jwks() -> dict[str, Any]:
    """v1 `Server.py:78-84`. v1 published the key of whichever gunicorn worker
    answered; this publishes every key the deployment might have signed with,
    so a rotation can overlap."""
    published = await keys.published_keys()
    return {"keys": [keys.public_jwk(k.private_pem, k.kid) for k in published]}


@router.get(
    "/.well-known/openid-configuration",
    summary="Discovery — where the token endpoint is and which grants it takes",
    responses={200: {"model": OpenIDConfiguration}},
    response_model=None,
)
async def openid_configuration(request: Request) -> dict[str, Any]:
    """v1 `Server.py:86-95`, plus what `x_miniapp_auth` added.

    The five v1 keys are unchanged and in place; the rest describe the token
    endpoint, its two Telegram grants and the revocation endpoint, which is
    what a Mini App or an OAuth client library discovers this deployment with.
    Additive by construction: a consumer written against v1 reads the same five
    values it always did.
    """
    base_url = _issuer(request)
    settings = get_settings()
    return {
        "issuer": base_url,
        "jwks_uri": f"{base_url}/.well-known/jwks.json",
        "response_types_supported": ["id_token"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["RS256"],
        "token_endpoint": f"{base_url}/oauth2/token",
        "revocation_endpoint": f"{base_url}/oauth2/revoke",
        "token_endpoint_auth_methods_supported": ["none"],
        "grant_types_supported": list(oauth.GRANTS),
        "scopes_supported": list(settings.miniapp_scopes),
    }


__all__ = ["JwksDocument", "OpenIDConfiguration", "router"]
