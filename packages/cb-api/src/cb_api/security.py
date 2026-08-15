"""Who is calling, and may they read this group — the other half of
`x_webhub_login`.

`cb_api.auth` mints a token; until now nothing verified one, because `/login`
was the only endpoint and it authenticates its *input* (a Telegram widget
payload) rather than a bearer token. `x_analytics_api` is the first endpoint
behind the token, so this is where verification and authorisation live.

## Verification

RS256 against the same keys `/.well-known/jwks.json` publishes
(`cb_api.keys.published_keys`), so a token signed by a key that has since
rotated out of the signing slot still verifies while it is still published —
which is the entire point of publishing more than one. `iss` is not checked:
the issuer is derived per request from the URL the client reached
(`routers/login._issuer`), so a deployment behind two names would reject its
own tokens. `exp` and `iat` are checked by PyJWT.

## Authorisation

A Telegram group's statistics are its admins' business. `group_admins` is
already maintained by `cb_core.admins` (it is what every admin-gated command
reads), so membership of that table for the requested `group_id` is the rule —
plus the tenant's own owners, who can already do anything to the brand
(`Tenant.owns`).

Note what this deliberately does **not** do: it never calls Telegram to
refresh the admin list. `cb_core.admins.refresh` needs a `Bot`, which cb-api
does not have and should not grow — an HTTP read is not the place to discover
that someone was promoted an hour ago. The row is written by the gateway on
every admin-gated command, so it is as fresh as the group's own activity.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

import jwt
from fastapi import Depends, HTTPException, Path, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from cb_api import keys
from cb_core import db, tenancy
from cb_core.logging import get_logger

log = get_logger("cb.api.security")

_bearer = HTTPBearer(auto_error=False)

_IS_ADMIN = """
SELECT 1
  FROM group_admins
 WHERE group_id = $1
   AND user_id = $2
 LIMIT 1
"""


def _ordered_keys(token: str, published: tuple[keys.SigningKey, ...]) -> list[keys.SigningKey]:
    """`published`, with the key the token's `kid` header names first.

    Every key is still tried — a `kid` is a hint from an unverified header, not
    a fact — but naming one correctly is the common case, so this makes the
    usual request cost one RSA verification instead of N.
    """
    try:
        kid = jwt.get_unverified_header(token).get("kid")
    except jwt.PyJWTError:
        return list(published)
    return sorted(published, key=lambda key: key.kid != kid)


async def _decode(token: str) -> dict[str, Any]:
    """The first published key that verifies wins. Every key is tried because a
    rotation publishes the outgoing one alongside the incoming one, and a token
    minted a minute before the swap has to keep working until it expires.

    `published_keys()` reads `signing_keys` per call when the PEM is not
    configured — a small coordinator-local table, and the same read
    `/.well-known/jwks.json` already does per request. Caching it would have to
    invalidate on rotation, which is exactly when being wrong costs the most,
    so the read stays.
    """
    last_error: Exception | None = None
    for key in _ordered_keys(token, await keys.published_keys()):
        public_pem = keys.public_pem(key.private_pem)
        try:
            return dict(
                jwt.decode(
                    token,
                    public_pem,
                    algorithms=["RS256"],
                    # `iss` varies with the URL the client reached, so it is not
                    # a fixed value this side can assert (module docstring).
                    options={"verify_iss": False, "verify_aud": False},
                )
            )
        except jwt.PyJWTError as exc:
            last_error = exc
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="invalid token",
        headers={"WWW-Authenticate": "Bearer"},
    ) from last_error


#: What a token with no `scope` claim may do. `/login` mints exactly such a
#: token and always has; reading was all the console could do before
#: `x_miniapp_auth` added write endpoints, so that is what it keeps. A caller
#: that needs more asks `/oauth2/token` for a scoped one.
LEGACY_SCOPES = frozenset({"groups:read"})


@dataclasses.dataclass(frozen=True, slots=True)
class Caller:
    """Who is on the other end of this request, and what they may do."""

    user_id: int
    scopes: frozenset[str]
    audience: str | None = None

    def can(self, scope: str) -> bool:
        return scope in self.scopes


async def current_caller(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Caller:
    """The verified token's subject and scopes, or 401.

    `auto_error=False` on the scheme so a missing header produces this
    function's own 401 with a `WWW-Authenticate` challenge rather than
    FastAPI's bare 403, which is what a browser needs to know it should log in
    again.
    """
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    claims = await _decode(credentials.credentials)
    subject = str(claims.get("sub", ""))
    try:
        user_id = int(subject)
    except ValueError:
        # v1 minted `sub` from the widget's `id`, always an integer. Anything
        # else is a token this deployment did not issue for a Telegram user.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid subject"
        ) from None

    raw_scope = claims.get("scope")
    scopes = frozenset(str(raw_scope).split()) if raw_scope else LEGACY_SCOPES
    audience = claims.get("aud")
    return Caller(user_id=user_id, scopes=scopes, audience=str(audience) if audience else None)


async def current_user(caller: Annotated[Caller, Depends(current_caller)]) -> int:
    """The Telegram user id alone, for endpoints that never look at scopes."""
    return caller.user_id


async def _is_group_admin(group_id: int, user_id: int) -> bool:
    row = await db.fetchrow(_IS_ADMIN, group_id, user_id, name="api_is_group_admin")
    return row is not None


async def administers(group_id: int, user_id: int) -> bool:
    """Group admin, or an owner of the tenant that group belongs to."""
    if await _is_group_admin(group_id, user_id):
        return True
    tenant = await tenancy.registry.by_id(tenancy.DEFAULT_TENANT)
    return bool(tenant.owns(user_id))


async def group_admin(
    group_id: Annotated[int, Path()],
    user_id: Annotated[int, Depends(current_user)],
) -> int:
    """Returns `group_id` once the caller is allowed to read it.

    404, not 403, for a group the caller does not administer: whether a given
    chat id is known to this deployment is not something an arbitrary logged-in
    user should be able to probe.
    """
    if await administers(group_id, user_id):
        return group_id
    log.info("api.analytics.denied", user_id=user_id)
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="group not found")


def group_admin_caller(*required: str) -> Callable[..., Awaitable[Caller]]:
    """Dependency: the caller, once they administer `{group_id}` **and** hold
    every named scope.

    Two failures, two different answers, on purpose:

    * not an admin of that group -> **404**, the same answer a group that does
      not exist gets, so a logged-in stranger cannot enumerate chat ids;
    * an admin whose token lacks the scope -> **403** with an
      `insufficient_scope` challenge (RFC 6750 §3.1), because the client can
      fix that by asking for a better token and needs to be told so.

    The order matters and is the less obvious half: membership is checked
    first, so a stranger never learns whether their scope would have been
    enough for a group they cannot see.
    """

    async def dependency(
        group_id: Annotated[int, Path()],
        caller: Annotated[Caller, Depends(current_caller)],
    ) -> Caller:
        if not await administers(group_id, caller.user_id):
            log.info("api.group.denied", user_id=caller.user_id, reason="not_admin")
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="group not found")
        missing = [scope for scope in required if not caller.can(scope)]
        if missing:
            log.info("api.group.denied", user_id=caller.user_id, reason="scope")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"token is missing scope: {' '.join(missing)}",
                headers={
                    "WWW-Authenticate": (
                        f'Bearer error="insufficient_scope", scope="{" ".join(required)}"'
                    )
                },
            )
        return caller

    return dependency


__all__ = [
    "LEGACY_SCOPES",
    "Caller",
    "administers",
    "current_caller",
    "current_user",
    "group_admin",
    "group_admin_caller",
]
