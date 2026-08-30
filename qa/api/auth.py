"""Telegram's two signatures, written out from Telegram's own documentation.

Every test in `qa/api/` needs a session, and the only way to get one is to
present something Telegram signed. So the suite signs it.

**This deliberately does not import `cb_api.miniapp`.** A fixture that builds
its input by calling the code under test can only ever agree with it: if the
data-check string grew a bug tomorrow, the module and its fixture would grow the
same bug and every test would still pass. Transcribed from the published
algorithm, these fail — which is the entire reason the duplication is here and
is the same rule `packages/cb-api/tests/conftest.py` follows.

`scripts/qa_setup.py` carries a third copy for the same reason and one more: it
runs in its own PEP 723 environment and cannot import this package at all.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import urlencode

MINIAPP_GRANT = "urn:cookiebot:params:oauth:grant-type:telegram-miniapp"
LOGIN_GRANT = "urn:cookiebot:params:oauth:grant-type:telegram-login"


def init_data(user_id: int, bot_token: str, *, auth_date: int | None = None) -> str:
    """A Mini App's `initData`, signed under `HMAC_SHA256("WebAppData", token)`.

    `hash` is the signature and is excluded from the string it signs; every
    other field is included, in sorted key order, newline-joined.
    """
    fields: dict[str, str] = {
        "auth_date": str(auth_date if auth_date is not None else int(time.time())),
        "query_id": "AAH-qa-api-suite",
        "user": json.dumps(
            {"id": user_id, "first_name": "QA", "username": f"qa{user_id}"},
            separators=(",", ":"),
        ),
    }
    check = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def widget_payload(
    user_id: int, bot_token: str, *, auth_date: int = 1_754_000_000
) -> dict[str, Any]:
    """The login widget's payload, signed under `sha256(token)` — a *different*
    key derivation from `initData`, which is why the two cannot be forged into
    each other and why `cb_api` keeps them in separate modules."""
    payload: dict[str, Any] = {"id": user_id, "first_name": "QA", "auth_date": auth_date}
    check = "\n".join(f"{key}={payload[key]}" for key in sorted(payload))
    secret = hashlib.sha256(bot_token.encode()).digest()
    payload["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return payload


__all__ = ["LOGIN_GRANT", "MINIAPP_GRANT", "init_data", "widget_payload"]
