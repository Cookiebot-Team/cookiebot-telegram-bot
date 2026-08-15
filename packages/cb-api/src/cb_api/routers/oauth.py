"""x_miniapp_auth — one token endpoint, three grants.

The Mini App needs a token, and so does the web console; both then call the
same resource endpoints. Rather than a second bespoke login route, this is an
OAuth2-shaped token endpoint (RFC 6749 §4 error bodies, RFC 7009 revocation),
with the two Telegram proofs modelled as extension grants:

| grant_type | proof | who uses it |
|---|---|---|
| `…:telegram-miniapp` | `init_data` — Telegram's `initData` string | the Mini App |
| `…:telegram-login` | `auth_data` — the login widget's JSON payload | the web console |
| `refresh_token` | a token this endpoint issued | both |

The access token is the same RS256 JWT `/login` mints, signed by the same keys
and published by the same JWKS, so nothing downstream learns a second format.
What is new is that it carries `scope` and `aud`, and that it is short-lived
with a rotating refresh token behind it (`cb_api.sessions`).

`/login` is untouched and still works. It is v1's endpoint, the shipped console
posts to it, and a compatibility route that quietly changed its token would be
the one break this codebase does not allow. A token from `/login` has no
`scope` claim, and `cb_api.security` reads that as read-only — which is exactly
what that console could do before this feature existed.

## Why the body is accepted as form *or* JSON

RFC 6749 says form-encoded, and every OAuth client library sends that. A Mini
App is a page calling `fetch` with a JSON body, and forcing it to build a form
just to satisfy a spec nobody else here reads would be ceremony. Both are
parsed; the response is JSON either way.

Which is also why the bodies below are declared to OpenAPI by hand
(`openapi_extra`) instead of as a signature parameter: FastAPI would pick one
content type and reject the other. The schema names both, and the handler keeps
accepting both.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel, Field

from cb_api import auth, keys, miniapp, sessions
from cb_core import metrics
from cb_core.logging import get_logger
from cb_core.settings import get_settings

log = get_logger("cb.api.oauth")

router = APIRouter(prefix="/oauth2", tags=["auth"])

MINIAPP_GRANT = "urn:cookiebot:params:oauth:grant-type:telegram-miniapp"
LOGIN_GRANT = "urn:cookiebot:params:oauth:grant-type:telegram-login"
REFRESH_GRANT = "refresh_token"

GRANTS = (MINIAPP_GRANT, LOGIN_GRANT, REFRESH_GRANT)


class TokenRequest(BaseModel):
    """The union of the three grants' bodies. Which fields are required depends
    on `grant_type`, which is why they are all optional here and checked in the
    handler — an OpenAPI schema cannot express "this one, then that one"
    without `oneOf` branches no generated client would read well."""

    grant_type: str = Field(description=" | ".join(GRANTS))
    init_data: str | None = Field(
        default=None,
        description="`window.Telegram.WebApp.initData`, verbatim — reordering or "
        "re-encoding it breaks the signature. Required for the miniapp grant.",
    )
    auth_data: dict[str, Any] | str | None = Field(
        default=None,
        description="the Telegram login widget's payload, as an object or its JSON "
        "string. Required for the login grant.",
    )
    refresh_token: str | None = Field(
        default=None,
        description="a refresh token this endpoint issued. Required for `refresh_token`.",
    )


class TokenResponse(BaseModel):
    """RFC 6749 §5.1. The access token is the same RS256 JWT `/login` mints,
    verifiable against `/.well-known/jwks.json`."""

    access_token: str
    token_type: str = Field(default="Bearer", examples=["Bearer"])
    expires_in: int = Field(description="access-token life in seconds")
    refresh_token: str = Field(
        description="rotates on every use; presenting a spent one revokes the whole family"
    )
    scope: str = Field(
        description="space-separated", examples=["groups:read groups:write audit:read"]
    )


class TokenError(BaseModel):
    """RFC 6749 §5.2. `invalid_grant` is one answer for a bad signature, a stale
    payload and a payload with no user in it, on purpose."""

    error: str = Field(examples=["invalid_grant"])
    error_description: str


class RevokeRequest(BaseModel):
    token: str = Field(description="an access or refresh token; unknown values succeed")


class RevokeResponse(BaseModel):
    revoked: bool


def _body_schema(model: type[BaseModel]) -> dict[str, Any]:
    """The request body, under both content types.

    Inlined rather than `$ref`-ed: nothing puts these models in
    `components/schemas` — they are never a parameter or a `response_model` —
    and a dangling reference is worse than a long schema.
    """
    schema = model.model_json_schema()
    return {
        "requestBody": {
            "required": True,
            "content": {
                "application/x-www-form-urlencoded": {"schema": schema},
                "application/json": {"schema": schema},
            },
        }
    }


async def _body(request: Request) -> dict[str, Any]:
    """Form-encoded or JSON, whichever arrived. Neither is an empty mapping,
    which every grant below rejects on its own terms."""
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("application/x-www-form-urlencoded"):
        return dict(await request.form())
    try:
        parsed = json.loads(await request.body() or b"{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _error(response: Response, code: str, *, http_status: int, description: str) -> dict[str, str]:
    """RFC 6749 §5.2. The description is for a developer reading a network tab;
    it never names which of several failures occurred when that difference is
    information about somebody else's token."""
    response.status_code = http_status
    return {"error": code, "error_description": description}


def _issue_access(
    *,
    subject: int,
    issuer: str,
    scope: str,
    audience: str,
    ttl_seconds: int,
    key: keys.SigningKey,
) -> str:
    claims = auth.build_claims(
        subject=str(subject),
        issuer=issuer,
        kid=key.kid,
        ttl_seconds=ttl_seconds,
        scope=scope,
        audience=audience,
        token_type="access",
    )
    return auth.issue_token(claims, key.private_pem, key.kid)


def issuer_for(request: Request) -> str:
    """Same rule as `/login`: the configured issuer, else the URL the client
    reached. `cb_api.security` deliberately does not verify `iss` for that
    reason."""
    configured = get_settings().webhub_issuer
    return configured or str(request.base_url).rstrip("/")


@router.post(
    "/token",
    summary="Exchange a Telegram proof — or a refresh token — for an access token",
    responses={
        200: {"model": TokenResponse, "description": "a fresh access token and its refresh token"},
        400: {
            "model": TokenError,
            "description": "`unsupported_grant_type`, `invalid_request` or `invalid_grant`",
        },
    },
    openapi_extra=_body_schema(TokenRequest),
    # The handler returns a token body *or* an error body, so the shapes are
    # declared per status above. `None` keeps FastAPI from also inferring the
    # signature's `dict[str, Any]` into the 200 and emitting a `$ref` with
    # `type: object` glued beside it, which generators read as two answers.
    response_model=None,
)
async def token(request: Request, response: Response) -> dict[str, Any]:
    """Exchange a Telegram proof — or a refresh token — for an access token."""
    settings = get_settings()
    form = await _body(request)
    grant = str(form.get("grant_type", ""))

    if grant not in GRANTS:
        metrics.auth_grants_rejected_total.labels(grant=grant or "none", reason="unsupported").inc()
        return _error(
            response,
            "unsupported_grant_type",
            http_status=status.HTTP_400_BAD_REQUEST,
            description=f"grant_type must be one of: {', '.join(GRANTS)}",
        )

    if grant == REFRESH_GRANT:
        return await _refresh(form, request, response)
    return await _telegram_grant(grant, form, request, response, settings=settings)


async def _telegram_grant(
    grant: str,
    form: dict[str, Any],
    request: Request,
    response: Response,
    *,
    settings: Any,
) -> dict[str, Any]:
    """The two proofs Telegram can give this deployment about a user.

    Both loop over every configured skin's bot token: one core serves several
    bots (`platform_tenancy`), a Mini App opened from any of them is a real
    session, and which token signed the payload is not something the client
    tells us.
    """
    tokens = tuple(settings.bot_tokens.values())
    if grant == MINIAPP_GRANT:
        raw = str(form.get("init_data", ""))
        if not raw:
            return _invalid_request(response, grant, "init_data is required")
        fields = miniapp.parse_init_data(raw)
        verified = any(miniapp.validate_init_data(fields, token) for token in tokens)
        fresh = miniapp.is_fresh(fields, settings.miniapp_init_data_max_age_seconds)
        subject = miniapp.user_id(fields)
    else:
        payload = form.get("auth_data")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = None
        if not isinstance(payload, dict) or not payload:
            return _invalid_request(response, grant, "auth_data is required")
        verified = any(auth.validate_telegram_auth(payload, token) for token in tokens)
        fresh = auth.is_fresh(payload, settings.webhub_auth_max_age_seconds)
        try:
            subject = int(payload.get("id", ""))
        except (TypeError, ValueError):
            subject = None

    if not verified or not fresh or subject is None:
        # One answer for a bad signature, a stale payload and a payload with no
        # user in it. A client that can tell them apart can probe which of its
        # forgeries got closest.
        reason = "unverified" if not verified else ("stale" if not fresh else "no_user")
        metrics.auth_grants_rejected_total.labels(grant=grant, reason=reason).inc()
        log.info("auth.grant_rejected", grant=grant, reason=reason)
        return _error(
            response,
            "invalid_grant",
            http_status=status.HTTP_400_BAD_REQUEST,
            description="the Telegram payload did not verify",
        )

    scope = " ".join(settings.miniapp_scopes)
    audience = settings.miniapp_audience
    key = await keys.signing_key()
    access = _issue_access(
        subject=subject,
        issuer=issuer_for(request),
        scope=scope,
        audience=audience,
        ttl_seconds=settings.miniapp_access_token_ttl_seconds,
        key=key,
    )
    refresh = await sessions.issue(
        user_id=subject,
        scope=scope,
        audience=audience,
        ttl_seconds=settings.miniapp_refresh_token_ttl_seconds,
    )
    metrics.auth_tokens_issued_total.labels(grant=grant).inc()
    log.info("auth.token_issued", grant=grant, user_id=subject)
    return _token_response(access, refresh.token, scope, settings.miniapp_access_token_ttl_seconds)


async def _refresh(form: dict[str, Any], request: Request, response: Response) -> dict[str, Any]:
    settings = get_settings()
    presented = str(form.get("refresh_token", ""))
    if not presented:
        return _invalid_request(response, REFRESH_GRANT, "refresh_token is required")

    session = await sessions.redeem(presented)
    if session is None:
        metrics.auth_grants_rejected_total.labels(grant=REFRESH_GRANT, reason="invalid").inc()
        return _error(
            response,
            "invalid_grant",
            http_status=status.HTTP_400_BAD_REQUEST,
            description="the refresh token is unknown, expired, revoked or already used",
        )

    key = await keys.signing_key()
    access = _issue_access(
        subject=session.user_id,
        issuer=issuer_for(request),
        scope=session.scope,
        audience=session.audience,
        ttl_seconds=settings.miniapp_access_token_ttl_seconds,
        key=key,
    )
    # Same family: rotation is a link in a chain, and the chain is what makes a
    # replay detectable.
    rotated = await sessions.issue(
        user_id=session.user_id,
        scope=session.scope,
        audience=session.audience,
        ttl_seconds=settings.miniapp_refresh_token_ttl_seconds,
        family_id=session.family_id,
    )
    metrics.auth_tokens_issued_total.labels(grant=REFRESH_GRANT).inc()
    return _token_response(
        access, rotated.token, session.scope, settings.miniapp_access_token_ttl_seconds
    )


def _invalid_request(response: Response, grant: str, description: str) -> dict[str, str]:
    metrics.auth_grants_rejected_total.labels(grant=grant, reason="malformed").inc()
    return _error(
        response,
        "invalid_request",
        http_status=status.HTTP_400_BAD_REQUEST,
        description=description,
    )


def _token_response(access: str, refresh: str, scope: str, ttl: int) -> dict[str, Any]:
    return {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": ttl,
        "refresh_token": refresh,
        "scope": scope,
    }


@router.post(
    "/revoke",
    summary="Revoke a refresh token (RFC 7009)",
    response_model=RevokeResponse,
    openapi_extra=_body_schema(RevokeRequest),
)
async def revoke(request: Request) -> dict[str, bool]:
    """RFC 7009. Revoking an unknown token is a success: the caller's goal is
    that the token cannot be used, and it cannot."""
    form = await _body(request)
    presented = str(form.get("token", ""))
    if presented:
        await sessions.revoke(presented)
    return {"revoked": True}


__all__ = [
    "GRANTS",
    "LOGIN_GRANT",
    "MINIAPP_GRANT",
    "REFRESH_GRANT",
    "TokenError",
    "TokenRequest",
    "TokenResponse",
    "issuer_for",
    "router",
]
