"""Unit coverage for `cb_api.auth` — the widget signature and the claim set.

Everything here is pure. The endpoints are
`packages/cb-api/tests/test_login_endpoints.py`; the key's durability is
`qa/integration/test_webhub_login.py`, which needs a database to prove
anything at all.

The vectors are computed with Telegram's published algorithm rather than
copied from a fixture, so a change to `data_check_string` cannot be
"corrected" by regenerating the expected value.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import jwt
import pytest

from cb_api import auth, keys

Payload = dict[str, Any]
#: `conftest.py`'s `sign` fixture. Spelled out rather than imported:
#: `packages/*/tests` are collected by path and are not importable packages.
Signer = Callable[[Payload, str], Payload]


class TestValidateTelegramAuth:
    def test_a_genuine_widget_payload_validates(
        self, payload: Payload, sign: Signer, bot_token: str
    ) -> None:
        assert auth.validate_telegram_auth(sign(payload, bot_token), bot_token)

    def test_another_bots_token_does_not(
        self, payload: Payload, sign: Signer, bot_token: str, other_token: str
    ) -> None:
        assert not auth.validate_telegram_auth(sign(payload, bot_token), other_token)

    def test_a_tampered_field_invalidates_it(
        self, payload: Payload, sign: Signer, bot_token: str
    ) -> None:
        signed = sign(payload, bot_token)
        signed["id"] = 1
        assert not auth.validate_telegram_auth(signed, bot_token)

    def test_a_payload_with_no_hash_is_refused(self, payload: Payload, bot_token: str) -> None:
        assert not auth.validate_telegram_auth(payload, bot_token)

    def test_an_empty_bot_token_never_validates(self, payload: Payload, sign: Signer) -> None:
        """An unconfigured skin must not become a skeleton key."""
        assert not auth.validate_telegram_auth(sign(payload, ""), "")

    def test_the_payload_is_not_mutated(
        self, payload: Payload, sign: Signer, bot_token: str, other_token: str
    ) -> None:
        """v1's own `auth_data.pop('hash')` (D-WL-2) meant the *second* token it
        tried saw a payload with no hash and failed immediately, so only the
        first configured bot could ever sign anyone in. Trying several tokens
        is the whole point of the loop, so the payload has to survive one."""
        signed = sign(payload, bot_token)
        assert not auth.validate_telegram_auth(signed, other_token)
        assert auth.validate_telegram_auth(signed, bot_token)
        assert "hash" in signed


class TestFreshness:
    def test_zero_max_age_accepts_anything(self, payload: Payload) -> None:
        """v1's behaviour, and this deployment's default."""
        assert auth.is_fresh({"auth_date": 0}, 0)
        assert auth.is_fresh(payload, 0, now=time.time())

    def test_a_recent_payload_passes_a_window(self) -> None:
        now = 1_754_000_000.0
        assert auth.is_fresh({"auth_date": int(now) - 30}, 86400, now=now)

    def test_an_old_payload_fails_a_window(self) -> None:
        now = 1_754_000_000.0
        assert not auth.is_fresh({"auth_date": int(now) - 90_000}, 86400, now=now)

    def test_a_payload_from_the_future_fails_a_window(self) -> None:
        now = 1_754_000_000.0
        assert not auth.is_fresh({"auth_date": int(now) + 600}, 86400, now=now)

    def test_a_missing_auth_date_fails_a_window_but_not_the_default(self) -> None:
        assert auth.is_fresh({}, 0)
        assert not auth.is_fresh({}, 86400)


class TestClaims:
    def test_v1s_claim_set_field_for_field(self) -> None:
        now = 1_754_000_000.0
        claims = auth.build_claims(
            subject="424243",
            issuer="https://api.example",
            kid="cookiebot-2025",
            ttl_seconds=1800,
            now=now,
        )
        assert claims == {
            "exp": 1_754_001_800,
            "iat": 1_754_000_000,
            "kid": "cookiebot-2025",
            "sub": "424243",
            "iss": "https://api.example",
        }

    def test_the_token_verifies_against_the_published_jwk(self) -> None:
        """The round trip a resource server actually performs: read the JWKS,
        build a key from it, verify. If `public_jwk` and `issue_token` ever
        disagree about the key, this is where it shows."""
        pem = keys.generate_private_pem()
        claims = auth.build_claims(
            subject="7", issuer="https://api.example", kid="k1", ttl_seconds=60
        )
        token = auth.issue_token(claims, pem, "k1")

        jwk = keys.public_jwk(pem, "k1")
        public = jwt.PyJWK(jwk).key
        decoded = jwt.decode(token, public, algorithms=["RS256"], issuer="https://api.example")

        assert decoded["sub"] == "7"
        assert jwt.get_unverified_header(token)["kid"] == "k1"

    def test_an_expired_token_is_rejected(self) -> None:
        pem = keys.generate_private_pem()
        claims = auth.build_claims(
            subject="7", issuer="i", kid="k1", ttl_seconds=1, now=time.time() - 3600
        )
        token = auth.issue_token(claims, pem, "k1")
        public = jwt.PyJWK(keys.public_jwk(pem, "k1")).key
        with pytest.raises(jwt.ExpiredSignatureError):
            jwt.decode(token, public, algorithms=["RS256"])
