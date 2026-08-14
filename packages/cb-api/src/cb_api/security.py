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


async def _decode(token: str) -> dict[str, Any]:
    """The first published key that verifies wins. Every key is tried because a
    rotation publishes the outgoing one alongside the incoming one, and a token
    minted a minute before the swap has to keep working until it expires."""
    last_error: Exception | None = None
    for key in await keys.published_keys():
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


async def current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> int:
    """The Telegram user id in `sub`, or 401.

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
        return int(subject)
    except ValueError:
        # v1 minted `sub` from the widget's `id`, always an integer. Anything
        # else is a token this deployment did not issue for a Telegram user.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid subject"
        ) from None


async def _is_group_admin(group_id: int, user_id: int) -> bool:
    row = await db.fetchrow(_IS_ADMIN, group_id, user_id, name="api_is_group_admin")
    return row is not None


async def group_admin(
    group_id: Annotated[int, Path()],
    user_id: Annotated[int, Depends(current_user)],
) -> int:
    """Returns `group_id` once the caller is allowed to read it.

    404, not 403, for a group the caller does not administer: whether a given
    chat id is known to this deployment is not something an arbitrary logged-in
    user should be able to probe.
    """
    if await _is_group_admin(group_id, user_id):
        return group_id
    tenant = await tenancy.registry.by_id(tenancy.DEFAULT_TENANT)
    if tenant.owns(user_id):
        return group_id
    log.info("api.analytics.denied", user_id=user_id)
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="group not found")


__all__ = ["current_user", "group_admin"]
