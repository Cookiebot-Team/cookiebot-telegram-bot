"""x_miniapp_auth — Telegram's `initData`, verified.

Net-new: v1 had no Mini App. The web console proves who it is with the **login
widget** (`cb_api.auth`), which is a JSON object signed under
`sha256(bot_token)`. A Mini App proves who it is with `initData`, which is a
query string signed under `HMAC_SHA256("WebAppData", bot_token)`. Same idea,
different key derivation and different framing — so they are different modules
rather than one function with a flag, and neither can be used to forge the
other.

Everything here is pure: a string and a token in, a decision out. No clock it
did not receive, no database, no HTTP.

## The three fields that are not part of the signature

`hash` is the signature itself. `signature` is Telegram's *third-party*
Ed25519 signature, present since Bot API 7.10 and explicitly excluded from the
HMAC check — a client that includes it in the data-check string computes a
different digest than Telegram did and rejects every real payload. Everything
else, including fields this codebase does not read, is signed and must be fed
back exactly as received: an unknown future field dropped here is a valid
payload that fails to verify.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import parse_qsl

#: Telegram's constant, not a secret (Bot API, "Validating data received via
#: the Mini App"). The bot token is the message; this is the key.
_WEBAPP_KEY = b"WebAppData"

_HASH_FIELD = "hash"
#: Excluded from the data-check string — see the module docstring.
_UNSIGNED_FIELDS = frozenset({_HASH_FIELD, "signature"})


def parse_init_data(raw: str) -> dict[str, str]:
    """`initData` as a flat mapping, values already percent-decoded.

    `keep_blank_values` matters: Telegram sends empty values for fields it has
    nothing for, they are part of what it signed, and dropping them changes the
    digest.
    """
    return dict(parse_qsl(raw, keep_blank_values=True, strict_parsing=False))


def data_check_string(fields: dict[str, str]) -> str:
    """`key=value` pairs, sorted by key, newline-joined, minus the two
    unsigned fields."""
    signed = {k: v for k, v in fields.items() if k not in _UNSIGNED_FIELDS}
    return "\n".join(f"{key}={signed[key]}" for key in sorted(signed))


def secret_key(bot_token: str) -> bytes:
    return hmac.new(_WEBAPP_KEY, bot_token.encode(), hashlib.sha256).digest()


def validate_init_data(fields: dict[str, str], bot_token: str) -> bool:
    """Whether this `initData` really came from Telegram, for this bot.

    `hmac.compare_digest`, not `==`, and a missing `hash` or an empty token is
    a failure rather than an exception — a caller loops over every configured
    skin's token, and one unconfigured skin must not break the loop.
    """
    provided = fields.get(_HASH_FIELD)
    if not provided or not bot_token:
        return False
    calculated = hmac.new(
        secret_key(bot_token), data_check_string(fields).encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(provided, calculated)


def is_fresh(fields: dict[str, str], max_age_seconds: int, *, now: float | None = None) -> bool:
    """Telegram's replay window over `auth_date`.

    Unlike the login widget's window this one is on by default (see the
    setting): there is no v1 behaviour to preserve, `initData` is re-issued
    every time the Mini App opens, and a captured payload that mints tokens
    forever is the failure this prevents. `max_age_seconds <= 0` disables it,
    for a deployment that decides otherwise.
    """
    if max_age_seconds <= 0:
        return True
    try:
        auth_date = int(fields.get("auth_date", ""))
    except ValueError:
        return False
    reference = time.time() if now is None else now
    # A payload stamped slightly in the future is a clock skew, not an attack;
    # one stamped far in the future would extend its own window, so it is not
    # allowed to.
    return -60 <= reference - auth_date <= max_age_seconds


def user(fields: dict[str, str]) -> dict[str, Any] | None:
    """The `user` field, parsed. `None` when absent or not an object.

    Telegram omits it when the Mini App was opened from an inline keyboard in a
    channel — those sessions have no user to authenticate, and this returning
    `None` is what turns that into a refusal upstream rather than a token for
    nobody.
    """
    raw = fields.get("user")
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def user_id(fields: dict[str, str]) -> int | None:
    """The Telegram id inside `user`, or `None`."""
    parsed = user(fields)
    if parsed is None:
        return None
    try:
        return int(parsed["id"])
    except (KeyError, TypeError, ValueError):
        return None


__all__ = [
    "data_check_string",
    "is_fresh",
    "parse_init_data",
    "secret_key",
    "user",
    "user_id",
    "validate_init_data",
]
