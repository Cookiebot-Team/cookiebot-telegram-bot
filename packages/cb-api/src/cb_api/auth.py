"""x_webhub_login — Telegram's login widget, verified, and the JWT it buys.

v1: `../COOKIEBOT-Telegram-Group-Bot/Bot/Server.py:25-52`
(`validate_telegram_auth`, `generate_jwt_token`). Contract:
`docs/contracts/x_webhub_login.md`. Spec:
`.specs/features/x_webhub_login/spec.md`.

Everything here is pure: bytes in, bytes out, no database and no clock it did
not receive. The key it signs with comes from `cb_api.keys`, the request that
triggers it from `cb_api.routers.login`.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any

import jwt

#: Telegram's own algorithm (`Server.py:31-37`): every field except `hash`,
#: `key=value` joined by newlines in sorted key order, HMAC-SHA256 under
#: `sha256(bot_token)`.
_HASH_FIELD = "hash"


def data_check_string(auth_data: dict[str, Any]) -> str:
    """v1's `"\\n".join(f"{key}={auth_data[key]}" for key in sorted(...))`
    (`Server.py:34`), over the payload with `hash` already removed.

    Values are stringified the way Flask's `get_json` would have produced them,
    which is what v1 interpolated: `auth_date` and `id` arrive as JSON numbers
    and `f"{...}"` renders them without quotes or decimal point. `str()` on the
    `int` FastAPI parsed does the same, and anything else is a string already.
    """
    return "\n".join(f"{key}={auth_data[key]}" for key in sorted(auth_data))


def validate_telegram_auth(auth_data: dict[str, Any], bot_token: str) -> bool:
    """Whether this payload really came from Telegram's widget for `bot_token`.

    v1 `Server.py:25-38`, with two differences and one deliberate sameness:

    * it does **not** mutate the caller's dict (v1's `auth_data.pop('hash')`
      did, which is why v1 could only ever try its token list in one order —
      the second call saw a payload with no hash and returned `False`
      immediately, so only the *first* configured token could ever match);
    * the comparison is `hmac.compare_digest`, not `==`;
    * `auth_date` is **not** checked here. v1 never did, and whether v2 does is
      a deployment setting (`webhub_auth_max_age_seconds`) applied by the
      caller, not a property of the signature.
    """
    provided_hash = auth_data.get(_HASH_FIELD)
    if not provided_hash or not bot_token:
        return False
    rest = {k: v for k, v in auth_data.items() if k != _HASH_FIELD}
    secret_key = hashlib.sha256(bot_token.encode()).digest()
    calculated = hmac.new(secret_key, data_check_string(rest).encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(str(provided_hash), calculated)


def is_fresh(auth_data: dict[str, Any], max_age_seconds: int, *, now: float | None = None) -> bool:
    """Telegram's replay window. `max_age_seconds <= 0` means "no window",
    which is v1's behaviour and this deployment's default — see the setting's
    own comment for why turning it on is a client change, not a config change.

    A payload with no parseable `auth_date` fails a window that is switched on.
    v1 would have accepted it, but v1 was not looking.
    """
    if max_age_seconds <= 0:
        return True
    raw = auth_data.get("auth_date")
    try:
        auth_date = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    reference = time.time() if now is None else now
    return 0 <= reference - auth_date <= max_age_seconds


def build_claims(
    *, subject: str, issuer: str, kid: str, ttl_seconds: int, now: float | None = None
) -> dict[str, Any]:
    """v1's claim set, field for field (`Server.py:41-48`) — including `kid`,
    which v1 put in the payload as well as (via `jwt.encode`) the header. It is
    unusual there; it is also what any consumer written against v1 reads.

    One departure, D-WL-4: v1 used `round(time.time())`, which for the 50% of
    calls landing past the half-second stamps `iat` **one second in the
    future**. A verifier that checks `iat` — PyJWT does, with zero leeway by
    default — rejects the token it was just handed, for up to a second. Floored
    here, so `iat` is never ahead of the clock; `exp` is still `iat + ttl`, so
    the token's life differs from v1's by at most that same second.
    """
    issued_at = int(time.time() if now is None else now)
    return {
        "exp": issued_at + ttl_seconds,
        "iat": issued_at,
        "kid": kid,
        "sub": subject,
        "iss": issuer,
    }


def issue_token(claims: dict[str, Any], private_pem: str, kid: str) -> str:
    """RS256, `kid` in the header — v1's `jwt.encode(..., algorithm="RS256")`
    with the key it had just built from its own JWK."""
    return jwt.encode(claims, private_pem, algorithm="RS256", headers={"kid": kid})


__all__ = [
    "build_claims",
    "data_check_string",
    "is_fresh",
    "issue_token",
    "validate_telegram_auth",
]
