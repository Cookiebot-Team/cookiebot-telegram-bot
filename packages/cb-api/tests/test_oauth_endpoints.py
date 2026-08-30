"""`/oauth2/token` and `/oauth2/revoke` — the three grants, over HTTP.

Same shape as `test_analytics_endpoints.py`: the router is mounted on a bare
app, the signing key is faked, and `cb_api.sessions` is replaced with an
in-memory store so this layer needs no database. What the store *does* —
rotation, replay detection, expiry — is proved against real rows in
`qa/integration/test_refresh_tokens.py`; what is under test here is the wire
contract, the error bodies and which claims end up in the token.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode
from uuid import uuid4

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cb_api import keys, sessions
from cb_api.routers import oauth
from cb_core.settings import get_settings

PEM = keys.generate_private_pem()
KID = "cookiebot-test"
BOT_TOKEN = "123456:AAH-fake-token-for-tests"
USER_ID = 424243
AUTH_DATE = 1_754_000_000


def _init_data(
    *, token: str = BOT_TOKEN, user_id: int | None = USER_ID, auth_date: int = AUTH_DATE
) -> str:
    fields: dict[str, str] = {"auth_date": str(auth_date), "query_id": "AAHdF6IQ"}
    if user_id is not None:
        fields["user"] = json.dumps({"id": user_id, "first_name": "T"}, separators=(",", ":"))
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def _widget_payload(*, token: str = BOT_TOKEN, user_id: int = USER_ID) -> dict[str, Any]:
    payload: dict[str, Any] = {"id": user_id, "first_name": "T", "auth_date": AUTH_DATE}
    check = "\n".join(f"{k}={payload[k]}" for k in sorted(payload))
    secret = hashlib.sha256(token.encode()).digest()
    payload["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return payload


class FakeSessions:
    """The refresh store, in a dict, with the same contract as the real one."""

    def __init__(self) -> None:
        self.rows: dict[str, sessions.Session] = {}
        self.reuse_detected = 0

    async def issue(
        self,
        *,
        user_id: int,
        scope: str,
        audience: str,
        ttl_seconds: int,
        family_id: Any = None,
        now: datetime | None = None,
    ) -> sessions.IssuedRefresh:
        issued_at = now or datetime.now(UTC)
        session = sessions.Session(
            family_id=family_id or uuid4(),
            user_id=user_id,
            scope=scope,
            audience=audience,
            issued_at=issued_at,
            expires_at=issued_at + timedelta(seconds=ttl_seconds),
        )
        token = f"refresh-{len(self.rows)}"
        self.rows[token] = session
        return sessions.IssuedRefresh(token=token, session=session)

    async def redeem(self, token: str, *, now: datetime | None = None) -> sessions.Session | None:
        session = self.rows.get(token)
        if session is None:
            return None
        if session.used_at is not None or session.revoked_at is not None:
            self.reuse_detected += 1
            for key, row in self.rows.items():
                if row.family_id == session.family_id:
                    self.rows[key] = dataclasses.replace(row, revoked_at=datetime.now(UTC))
            return None
        self.rows[token] = dataclasses.replace(session, used_at=now or datetime.now(UTC))
        return self.rows[token]

    async def revoke(self, token: str, *, now: datetime | None = None) -> bool:
        session = self.rows.get(token)
        if session is None:
            return False
        self.rows[token] = dataclasses.replace(session, revoked_at=now or datetime.now(UTC))
        return True


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _signing() -> keys.SigningKey:
        return keys.SigningKey(kid=KID, private_pem=PEM)

    monkeypatch.setattr(keys, "signing_key", _signing)


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "bot_tokens", {"cookiebot": BOT_TOKEN}, raising=False)
    monkeypatch.setattr(settings, "webhub_issuer", "https://api.example.test", raising=False)
    monkeypatch.setattr(settings, "miniapp_init_data_max_age_seconds", 0, raising=False)
    monkeypatch.setattr(settings, "webhub_auth_max_age_seconds", 0, raising=False)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> FakeSessions:
    fake = FakeSessions()
    monkeypatch.setattr(oauth.sessions, "issue", fake.issue)
    monkeypatch.setattr(oauth.sessions, "redeem", fake.redeem)
    monkeypatch.setattr(oauth.sessions, "revoke", fake.revoke)
    return fake


@pytest.fixture
def client(store: FakeSessions) -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(oauth.router)
    with TestClient(app) as test_client:
        yield test_client


def _claims(token: str) -> dict[str, Any]:
    return dict(
        jwt.decode(
            token,
            keys.public_pem(PEM),
            algorithms=["RS256"],
            options={"verify_aud": False, "verify_iss": False},
        )
    )


# ------------------------------------------------------------- the Mini App


def test_init_data_buys_an_access_token(client: TestClient) -> None:
    response = client.post(
        "/oauth2/token",
        json={"grant_type": oauth.MINIAPP_GRANT, "init_data": _init_data()},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] > 0
    assert body["refresh_token"]
    claims = _claims(body["access_token"])
    assert claims["sub"] == str(USER_ID)
    assert claims["iss"] == "https://api.example.test"
    assert claims["typ"] == "access"
    assert "groups:write" in claims["scope"].split()


def test_a_form_encoded_body_works_too(client: TestClient) -> None:
    """Every OAuth client library posts a form; the Mini App posts JSON."""
    response = client.post(
        "/oauth2/token",
        data={"grant_type": oauth.MINIAPP_GRANT, "init_data": _init_data()},
    )
    assert response.status_code == 200


def test_init_data_signed_by_another_bot_is_invalid_grant(client: TestClient) -> None:
    response = client.post(
        "/oauth2/token",
        json={"grant_type": oauth.MINIAPP_GRANT, "init_data": _init_data(token="999:other")},
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_grant"


def test_stale_init_data_is_refused_when_the_window_is_on(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "miniapp_init_data_max_age_seconds", 60, raising=False)
    response = client.post(
        "/oauth2/token",
        json={"grant_type": oauth.MINIAPP_GRANT, "init_data": _init_data(auth_date=1)},
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_grant"


def test_init_data_without_a_user_gets_no_token(client: TestClient) -> None:
    response = client.post(
        "/oauth2/token",
        json={"grant_type": oauth.MINIAPP_GRANT, "init_data": _init_data(user_id=None)},
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_grant"


def test_a_missing_init_data_is_invalid_request(client: TestClient) -> None:
    response = client.post("/oauth2/token", json={"grant_type": oauth.MINIAPP_GRANT})
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"


# ------------------------------------------------------------- the console


def test_the_login_widget_payload_is_a_grant_too(client: TestClient) -> None:
    response = client.post(
        "/oauth2/token",
        json={"grant_type": oauth.LOGIN_GRANT, "auth_data": _widget_payload()},
    )
    assert response.status_code == 200
    assert _claims(response.json()["access_token"])["sub"] == str(USER_ID)


def test_the_widget_payload_may_arrive_as_a_json_string(client: TestClient) -> None:
    """A form post cannot nest an object, so the console sends it encoded."""
    response = client.post(
        "/oauth2/token",
        data={"grant_type": oauth.LOGIN_GRANT, "auth_data": json.dumps(_widget_payload())},
    )
    assert response.status_code == 200


# ------------------------------------------------------------------ refresh


def test_a_refresh_token_buys_a_new_pair(client: TestClient) -> None:
    first = client.post(
        "/oauth2/token", json={"grant_type": oauth.MINIAPP_GRANT, "init_data": _init_data()}
    ).json()
    second = client.post(
        "/oauth2/token",
        json={"grant_type": "refresh_token", "refresh_token": first["refresh_token"]},
    )
    assert second.status_code == 200
    body = second.json()
    assert body["refresh_token"] != first["refresh_token"]  # rotated
    assert _claims(body["access_token"])["sub"] == str(USER_ID)
    assert body["scope"] == first["scope"]


def test_a_replayed_refresh_token_is_refused_and_kills_the_family(
    client: TestClient, store: FakeSessions
) -> None:
    first = client.post(
        "/oauth2/token", json={"grant_type": oauth.MINIAPP_GRANT, "init_data": _init_data()}
    ).json()
    rotated = client.post(
        "/oauth2/token",
        json={"grant_type": "refresh_token", "refresh_token": first["refresh_token"]},
    ).json()

    replay = client.post(
        "/oauth2/token",
        json={"grant_type": "refresh_token", "refresh_token": first["refresh_token"]},
    )
    assert replay.status_code == 400
    assert store.reuse_detected == 1

    # And the token the honest client is holding is dead too — that is the
    # trade the family revocation makes.
    after = client.post(
        "/oauth2/token",
        json={"grant_type": "refresh_token", "refresh_token": rotated["refresh_token"]},
    )
    assert after.status_code == 400


def test_an_unknown_refresh_token_is_invalid_grant(client: TestClient) -> None:
    response = client.post(
        "/oauth2/token", json={"grant_type": "refresh_token", "refresh_token": "nope"}
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_grant"


def test_an_unsupported_grant_names_the_supported_ones(client: TestClient) -> None:
    response = client.post("/oauth2/token", json={"grant_type": "password"})
    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "unsupported_grant_type"
    assert oauth.MINIAPP_GRANT in body["error_description"]


def test_an_empty_body_is_not_a_500(client: TestClient) -> None:
    response = client.post(
        "/oauth2/token", content=b"", headers={"content-type": "application/json"}
    )
    assert response.status_code == 400


# ----------------------------------------------------------------- revoke


def test_revoking_a_token_stops_it_refreshing(client: TestClient) -> None:
    issued = client.post(
        "/oauth2/token", json={"grant_type": oauth.MINIAPP_GRANT, "init_data": _init_data()}
    ).json()
    assert client.post("/oauth2/revoke", json={"token": issued["refresh_token"]}).status_code == 200
    response = client.post(
        "/oauth2/token",
        json={"grant_type": "refresh_token", "refresh_token": issued["refresh_token"]},
    )
    assert response.status_code == 400


def test_revoking_an_unknown_token_is_a_success(client: TestClient) -> None:
    """RFC 7009: the caller's goal is that the token cannot be used, and it
    cannot."""
    assert client.post("/oauth2/revoke", json={"token": "never-issued"}).status_code == 200


# ------------------------------------------------------- the admin scope grant


@pytest.fixture
def owner(monkeypatch: pytest.MonkeyPatch) -> None:
    """`USER_ID` runs this deployment. Patched at the predicate rather than by
    building a tenant row, because *which* of the two owner sources said so is
    `test_admin_endpoints.py`'s subject, not this one's."""

    async def _is_owner(user_id: int) -> bool:
        return user_id == USER_ID

    monkeypatch.setattr(oauth.security, "is_bot_admin", _is_owner)


def test_an_owners_session_is_granted_the_admin_scope(client: TestClient, owner: None) -> None:
    body = client.post(
        "/oauth2/token", json={"grant_type": oauth.MINIAPP_GRANT, "init_data": _init_data()}
    ).json()
    assert "admin:read" in body["scope"].split()
    assert "admin:read" in _claims(body["access_token"])["scope"].split()


def test_everyone_elses_session_is_not(client: TestClient, owner: None) -> None:
    """The scope is granted, never assumed: a non-owner's token cannot reach
    `/admin/...` however the client edits its own request, because the claim
    the endpoint reads was never minted."""
    body = client.post(
        "/oauth2/token",
        json={"grant_type": oauth.MINIAPP_GRANT, "init_data": _init_data(user_id=1234)},
    ).json()
    assert "admin:read" not in body["scope"].split()
    assert "groups:write" in body["scope"].split()


def test_a_refresh_reissues_the_scope_the_session_was_granted(
    client: TestClient, owner: None
) -> None:
    """Not re-evaluated per refresh, deliberately — see `oauth._scopes_for`.
    An owner removed today keeps the scope until the refresh token expires or
    the session is revoked, and that bound is what makes the TTL a security
    setting rather than a convenience one."""
    issued = client.post(
        "/oauth2/token", json={"grant_type": oauth.MINIAPP_GRANT, "init_data": _init_data()}
    ).json()
    refreshed = client.post(
        "/oauth2/token",
        json={"grant_type": "refresh_token", "refresh_token": issued["refresh_token"]},
    ).json()
    assert refreshed["scope"] == issued["scope"]
