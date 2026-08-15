"""`cb_api.routers.login` over HTTP — the four responses `COOKIEBOT-WebHub` reads.

The router is mounted on a bare app rather than `cb_api.main`'s, so this layer
stays free of the pool, the cache and object storage: what it asserts is the
wire contract, and the wire contract does not depend on any of them. The
database-backed half — that the signing key survives a restart and is shared
between replicas — is `qa/integration/test_webhub_login.py`, because that is
the only place it can be true.

`../COOKIEBOT-WebHub/src/lib/api/axios.ts` reads `accessToken` from the body
and `exp` from inside the token; both are asserted here by name.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cb_api import keys
from cb_api.routers import login
from cb_core.settings import Settings

#: `conftest.py`'s `sign` fixture — see the note in `test_webhub_auth.py`.
Signer = Callable[[dict[str, Any], str], dict[str, Any]]

PEM = keys.generate_private_pem()


@pytest.fixture
def settings(bot_token: str, other_token: str) -> Settings:
    return Settings(
        service_name="cb-api-test",
        traces_enabled=False,
        bot_tokens={"cookiebot": bot_token, "bombot": other_token},
        webhub_jwt_private_key_pem=PEM,
        webhub_jwt_kid="cookiebot-2025",
        webhub_issuer="https://api.example",
    )


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[TestClient]:
    monkeypatch.setattr(login, "get_settings", lambda: settings)
    monkeypatch.setattr(keys, "get_settings", lambda: settings)
    keys.reset_cache()

    async def _count() -> int:
        return 1275

    monkeypatch.setattr(login.ops, "count_groups", _count)
    app = FastAPI()
    app.include_router(login.router)
    with TestClient(app) as c:
        yield c
    keys.reset_cache()


class TestLogin:
    def test_a_widget_payload_buys_a_token(
        self, client: TestClient, payload: dict[str, Any], sign: Signer, bot_token: str
    ) -> None:
        response = client.post("/login", json=sign(payload, bot_token))
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "Token generated"  # v1's own wording
        decoded = jwt.decode(
            body["accessToken"],
            jwt.PyJWK(keys.public_jwk(PEM, "cookiebot-2025")).key,
            algorithms=["RS256"],
            issuer="https://api.example",
        )
        assert decoded["sub"] == "424243"
        assert decoded["kid"] == "cookiebot-2025"
        assert decoded["exp"] - decoded["iat"] == 1800  # v1's 30 minutes

    def test_any_configured_skins_token_signs_you_in(
        self, client: TestClient, payload: dict[str, Any], sign: Signer, other_token: str
    ) -> None:
        """v1 looped over five tokens but could only ever match the first
        (D-WL-2). The second bot's users could not log in at all."""
        assert client.post("/login", json=sign(payload, other_token)).status_code == 200

    def test_an_unknown_token_gets_v1s_401(
        self, client: TestClient, payload: dict[str, Any], sign: Signer
    ) -> None:
        response = client.post("/login", json=sign(payload, "555:nobody"))
        assert response.status_code == 401
        assert response.json() == {"error": "Invalid bot token"}

    def test_an_empty_body_gets_v1s_400(self, client: TestClient) -> None:
        response = client.post("/login", json={})
        assert response.status_code == 400
        assert response.json() == {"error": "Missing data"}

    def test_an_unsigned_payload_is_refused(
        self, client: TestClient, payload: dict[str, Any]
    ) -> None:
        assert client.post("/login", json=payload).status_code == 401

    def test_a_stale_payload_is_refused_once_a_window_is_configured(
        self,
        settings: Settings,
        client: TestClient,
        payload: dict[str, Any],
        sign: Signer,
        bot_token: str,
    ) -> None:
        """The replay hole v1 left open, closed — and off by default, because
        the shipped client renews by re-posting this very payload."""
        assert client.post("/login", json=sign(payload, bot_token)).status_code == 200
        settings.webhub_auth_max_age_seconds = 86400
        assert client.post("/login", json=sign(payload, bot_token)).status_code == 401


class TestDiscovery:
    def test_the_jwks_verifies_a_token_this_service_issued(
        self, client: TestClient, payload: dict[str, Any], sign: Signer, bot_token: str
    ) -> None:
        token = client.post("/login", json=sign(payload, bot_token)).json()["accessToken"]
        published = client.get("/.well-known/jwks.json").json()["keys"]
        key = jwt.PyJWKSet.from_dict({"keys": published}).keys[0]
        assert jwt.decode(token, key.key, algorithms=["RS256"], issuer="https://api.example")

    def test_the_jwks_never_publishes_the_private_half(self, client: TestClient) -> None:
        published = client.get("/.well-known/jwks.json").json()["keys"]
        assert published
        for jwk in published:
            assert jwk["kty"] == "RSA"
            assert set(jwk) & {"d", "p", "q", "dp", "dq", "qi"} == set()

    def test_openid_configuration_still_carries_v1s_five_keys(self, client: TestClient) -> None:
        """`x_miniapp_auth` added the token endpoint to this document. The five
        v1 keys are asserted by value rather than the whole document by
        equality: a consumer written against v1 reads exactly these, and adding
        a key is not a break — changing one is."""
        document = client.get("/.well-known/openid-configuration").json()
        assert {
            key: document[key]
            for key in (
                "issuer",
                "jwks_uri",
                "response_types_supported",
                "subject_types_supported",
                "id_token_signing_alg_values_supported",
            )
        } == {
            "issuer": "https://api.example",
            "jwks_uri": "https://api.example/.well-known/jwks.json",
            "response_types_supported": ["id_token"],
            "subject_types_supported": ["public"],
            "id_token_signing_alg_values_supported": ["RS256"],
        }

    def test_openid_configuration_advertises_the_token_endpoint(self, client: TestClient) -> None:
        """What a Mini App or an OAuth client library discovers this deployment
        with."""
        document = client.get("/.well-known/openid-configuration").json()
        assert document["token_endpoint"] == "https://api.example/oauth2/token"
        assert document["revocation_endpoint"] == "https://api.example/oauth2/revoke"
        assert "refresh_token" in document["grant_types_supported"]
        assert "groups:write" in document["scopes_supported"]

    def test_the_issuer_falls_back_to_the_request_when_unconfigured(
        self, settings: Settings, client: TestClient
    ) -> None:
        """v1 had no setting at all — its issuer was `request.url_root`, i.e.
        the `Host` header. Reproduced when unset so nothing breaks; setting it
        is what stops a caller choosing the issuer."""
        settings.webhub_issuer = ""
        assert client.get("/.well-known/openid-configuration").json()["issuer"] == (
            "http://testserver"
        )


class TestHome:
    def test_home_reports_the_real_group_count(self, client: TestClient) -> None:
        """v1 answered the module constant `NUMBER_CHATS = 1275`, which nothing
        ever updated. Here the number comes from `groups`."""
        assert client.get("/").json() == {"status": "Bot is online", "number_chats": 1275}
