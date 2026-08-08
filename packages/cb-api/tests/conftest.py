"""Shared fixtures for `x_webhub_login`'s unit layer.

`sign` is a fixture rather than a helper the other modules import, because
`packages/cb-api/tests` is not an importable package (there is no `__init__.py`
anywhere under `packages/*/tests`, by design — pytest collects them by path).
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable
from typing import Any

import pytest

#: Shaped like a real bot token, and deliberately not one.
BOT_TOKEN = "123456:AAH-fake-token-for-tests"
OTHER_TOKEN = "999999:BBH-some-other-bot"

Signer = Callable[[dict[str, Any], str], dict[str, Any]]


@pytest.fixture
def bot_token() -> str:
    return BOT_TOKEN


@pytest.fixture
def other_token() -> str:
    return OTHER_TOKEN


@pytest.fixture
def sign() -> Signer:
    """Telegram's login widget, as the widget itself would have produced it.

    Written out from Telegram's published algorithm rather than by calling
    `cb_api.auth`, so a change to the module under test cannot make its own
    fixtures agree with it.
    """

    def _sign(payload: dict[str, Any], token: str) -> dict[str, Any]:
        check = "\n".join(f"{k}={payload[k]}" for k in sorted(payload))
        secret = hashlib.sha256(token.encode()).digest()
        digest = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        return {**payload, "hash": digest}

    return _sign


@pytest.fixture
def payload() -> dict[str, Any]:
    return {
        "id": 424243,
        "first_name": "Tester",
        "username": "tester",
        "photo_url": "https://t.me/i/userpic/320/tester.jpg",
        "auth_date": 1_754_000_000,
    }
