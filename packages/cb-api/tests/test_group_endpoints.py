"""`cb_api.routers.groups` — who may read a group's settings, who may change
them, and what the change leaves behind.

The authorisation boundary is the point of this module: three callers (an
admin, a tenant owner, a stranger) against the same group, plus the scope
split between reading and writing. The database is faked; the rows those writes
really produce are asserted in `qa/integration/test_audit_log.py`.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cb_api import keys, security
from cb_api.routers import groups as router_module
from cb_core import audit, group_config, group_texts, tenancy

PEM = keys.generate_private_pem()
KID = "cookiebot-test"

GROUP_ID = -1001234567890
OTHER_GROUP = -1009999999999
ADMIN_ID = 4242
STRANGER_ID = 9999
OWNER_ID = 777

READ_WRITE = "groups:read groups:write audit:read"


def _token(subject: int, *, scope: str | None = READ_WRITE) -> str:
    claims: dict[str, Any] = {
        "sub": str(subject),
        "iat": 1_700_000_000,
        "exp": 4_102_444_800,
        "kid": KID,
    }
    if scope is not None:
        claims["scope"] = scope
    return jwt.encode(claims, PEM, algorithm="RS256", headers={"kid": KID})


def _auth(subject: int = ADMIN_ID, *, scope: str | None = READ_WRITE) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(subject, scope=scope)}"}


@pytest.fixture(autouse=True)
def _keys(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _published() -> tuple[keys.SigningKey, ...]:
        return (keys.SigningKey(kid=KID, private_pem=PEM),)

    monkeypatch.setattr(keys, "published_keys", _published)


@pytest.fixture(autouse=True)
def _admins(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _is_admin(group_id: int, user_id: int) -> bool:
        return group_id == GROUP_ID and user_id == ADMIN_ID

    monkeypatch.setattr(security, "_is_group_admin", _is_admin)

    async def _tenant(tenant_id: str) -> tenancy.Tenant:
        return tenancy.Tenant(
            tenant_id=tenancy.DEFAULT_TENANT, display_name="Cookiebot", owner_ids=(OWNER_ID,)
        )

    monkeypatch.setattr(tenancy.registry, "by_id", _tenant)


class FakeState:
    """The three tables this router touches, in memory."""

    def __init__(self) -> None:
        self.config = dataclasses.replace(group_config.DEFAULTS, group_id=GROUP_ID)
        self.rules: group_texts.GroupText | None = None
        self.welcome: group_texts.GroupText | None = None
        self.audit: list[audit.AuditEvent] = []


@pytest.fixture
def state(monkeypatch: pytest.MonkeyPatch) -> FakeState:
    fake = FakeState()

    async def _get_config(group_id: int) -> group_config.GroupConfig:
        return fake.config

    async def _set_config(group_id: int, **fields: Any) -> group_config.GroupConfig:
        fake.config = dataclasses.replace(fake.config, **fields)
        return fake.config

    async def _get_rules(group_id: int) -> group_texts.GroupText | None:
        return fake.rules

    async def _set_rules(group_id: int, body: str, *, updated_by: int | None = None) -> None:
        fake.rules = group_texts.GroupText(
            body=body, updated_by=updated_by, updated_at=datetime.now(UTC)
        )

    async def _get_welcome(group_id: int) -> group_texts.GroupText | None:
        return fake.welcome

    async def _set_welcome(group_id: int, body: str, *, updated_by: int | None = None) -> None:
        fake.welcome = group_texts.GroupText(
            body=body, updated_by=updated_by, updated_at=datetime.now(UTC)
        )

    async def _record(group_id: int, action: str, **kwargs: Any) -> audit.AuditEvent:
        from cb_core import ids

        event = audit.AuditEvent(
            id=ids.uuid7(),
            group_id=group_id,
            ts=datetime.now(UTC),
            action=action,
            surface=kwargs.get("surface", "api"),
            actor_user_id=kwargs.get("actor_user_id"),
            summary=kwargs.get("summary"),
            before=kwargs.get("before"),
            after=kwargs.get("after"),
        )
        fake.audit.append(event)
        return event

    async def _page(group_id: int, **kwargs: Any) -> tuple[audit.AuditEvent, ...]:
        rows = [e for e in reversed(fake.audit) if e.group_id == group_id]
        if kwargs.get("action"):
            rows = [e for e in rows if e.action == kwargs["action"]]
        return tuple(rows[: kwargs.get("limit", 50)])

    async def _my_groups(stmt: str, *args: Any, name: str = "") -> list[dict[str, Any]]:
        user_id = args[0]
        if user_id != ADMIN_ID:
            return []
        return [
            {
                "group_id": GROUP_ID,
                "title": "Test Group",
                "username": None,
                "chat_type": "supergroup",
                "role": "creator",
                "anonymous": False,
            }
        ]

    monkeypatch.setattr(router_module.group_config, "get_config", _get_config)
    monkeypatch.setattr(router_module.group_config, "set_config", _set_config)
    monkeypatch.setattr(router_module.group_texts, "get_rules", _get_rules)
    monkeypatch.setattr(router_module.group_texts, "set_rules", _set_rules)
    monkeypatch.setattr(router_module.group_texts, "get_welcome", _get_welcome)
    monkeypatch.setattr(router_module.group_texts, "set_welcome", _set_welcome)
    monkeypatch.setattr(router_module.audit, "record", _record)
    monkeypatch.setattr(router_module.audit, "page", _page)
    monkeypatch.setattr(router_module.db, "fetch", _my_groups)
    return fake


@pytest.fixture
def client(state: FakeState) -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(router_module.router)
    with TestClient(app) as test_client:
        yield test_client


# ------------------------------------------------------------ who gets in


def test_an_admin_reads_the_config(client: TestClient) -> None:
    response = client.get(f"/groups/{GROUP_ID}/config", headers=_auth())
    assert response.status_code == 200
    assert response.json()["config"]["captcha_timeout_seconds"] == 300


def test_a_tenant_owner_reads_any_group(client: TestClient) -> None:
    response = client.get(f"/groups/{GROUP_ID}/config", headers=_auth(OWNER_ID))
    assert response.status_code == 200


def test_a_stranger_gets_404_not_403(client: TestClient) -> None:
    """Whether a chat id is known to this deployment is not something a
    logged-in stranger may probe."""
    response = client.get(f"/groups/{GROUP_ID}/config", headers=_auth(STRANGER_ID))
    assert response.status_code == 404


def test_an_admin_of_one_group_is_a_stranger_to_another(client: TestClient) -> None:
    response = client.get(f"/groups/{OTHER_GROUP}/config", headers=_auth(ADMIN_ID))
    assert response.status_code == 404


def test_no_token_is_401_with_a_challenge(client: TestClient) -> None:
    response = client.get(f"/groups/{GROUP_ID}/config")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_a_legacy_console_token_may_read_but_not_write(client: TestClient) -> None:
    """`/login` mints a token with no `scope`; that is read-only by
    construction (`security.LEGACY_SCOPES`)."""
    legacy = _auth(ADMIN_ID, scope=None)
    assert client.get(f"/groups/{GROUP_ID}/config", headers=legacy).status_code == 200
    response = client.patch(f"/groups/{GROUP_ID}/config", json={"sfw": False}, headers=legacy)
    assert response.status_code == 403
    assert "insufficient_scope" in response.headers["www-authenticate"]


def test_a_stranger_with_every_scope_still_gets_404(client: TestClient) -> None:
    """Membership is checked before scope, so a scope error never confirms that
    a group exists."""
    response = client.patch(
        f"/groups/{GROUP_ID}/config", json={"sfw": False}, headers=_auth(STRANGER_ID)
    )
    assert response.status_code == 404


def test_the_audit_trail_needs_its_own_scope(client: TestClient) -> None:
    response = client.get(f"/groups/{GROUP_ID}/audit", headers=_auth(ADMIN_ID, scope="groups:read"))
    assert response.status_code == 403


# --------------------------------------------------------------- changing


def test_a_patch_changes_only_what_it_names(client: TestClient, state: FakeState) -> None:
    before_limit = state.config.sticker_spam_limit
    response = client.patch(
        f"/groups/{GROUP_ID}/config",
        json={"captcha_timeout_seconds": 600},
        headers=_auth(),
    )
    assert response.status_code == 200
    assert response.json()["changed"] == ["captcha_timeout_seconds"]
    assert state.config.captcha_timeout_seconds == 600
    assert state.config.sticker_spam_limit == before_limit


def test_a_change_writes_an_audit_row_with_both_values(
    client: TestClient, state: FakeState
) -> None:
    client.patch(f"/groups/{GROUP_ID}/config", json={"functions_fun": False}, headers=_auth())
    assert len(state.audit) == 1
    event = state.audit[0]
    assert event.action == audit.CONFIG_UPDATED
    assert event.actor_user_id == ADMIN_ID
    assert event.surface == "miniapp"
    assert event.before == {"functions_fun": True}
    assert event.after == {"functions_fun": False}


def test_saving_an_unchanged_form_records_nothing(client: TestClient, state: FakeState) -> None:
    """A Mini App that PATCHes the value already stored has not changed
    anything, and an audit trail full of no-ops is a trail nobody reads."""
    client.patch(f"/groups/{GROUP_ID}/config", json={"sfw": True}, headers=_auth())
    assert state.audit == []


def test_an_empty_patch_is_a_400(client: TestClient) -> None:
    response = client.patch(f"/groups/{GROUP_ID}/config", json={}, headers=_auth())
    assert response.status_code == 400


def test_an_unknown_field_is_refused(client: TestClient) -> None:
    """`extra="forbid"`: a typo silently ignored is a setting the admin
    believes they changed."""
    response = client.patch(
        f"/groups/{GROUP_ID}/config", json={"captcha_timeout": 60}, headers=_auth()
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    "patch",
    [
        {"captcha_timeout_seconds": -1},
        {"sticker_spam_limit": 0},
        {"max_posts": 10_000},
        {"language": "klingon"},
    ],
)
def test_out_of_range_values_are_refused(client: TestClient, patch: dict[str, Any]) -> None:
    assert (
        client.patch(f"/groups/{GROUP_ID}/config", json=patch, headers=_auth()).status_code == 422
    )


@pytest.mark.parametrize(("sent", "stored"), [("pt", "pt"), ("eng", "en"), ("es", "es")])
def test_the_menus_language_spellings_are_accepted(
    client: TestClient, state: FakeState, sent: str, stored: str
) -> None:
    response = client.patch(f"/groups/{GROUP_ID}/config", json={"language": sent}, headers=_auth())
    assert response.status_code == 200
    assert state.config.language == stored


# ----------------------------------------------------------- rules, welcome


def test_rules_round_trip_and_leave_a_row(client: TestClient, state: FakeState) -> None:
    assert client.get(f"/groups/{GROUP_ID}/rules", headers=_auth()).json()["body"] is None
    response = client.put(f"/groups/{GROUP_ID}/rules", json={"body": "1. be nice"}, headers=_auth())
    assert response.status_code == 200
    assert response.json()["body"] == "1. be nice"
    assert response.json()["updated_by"] == ADMIN_ID
    assert state.audit[-1].action == audit.RULES_UPDATED


def test_the_welcome_message_is_stored_verbatim(client: TestClient, state: FakeState) -> None:
    """Placeholders are substituted when the message is sent, not here."""
    body = "Welcome <user>, read the /rules"
    client.put(f"/groups/{GROUP_ID}/welcome", json={"body": body}, headers=_auth())
    assert state.welcome is not None
    assert state.welcome.body == body


def test_an_empty_body_is_refused(client: TestClient) -> None:
    response = client.put(f"/groups/{GROUP_ID}/rules", json={"body": ""}, headers=_auth())
    assert response.status_code == 422


def test_a_body_longer_than_telegram_allows_is_refused(client: TestClient) -> None:
    response = client.put(f"/groups/{GROUP_ID}/rules", json={"body": "x" * 5000}, headers=_auth())
    assert response.status_code == 422


# --------------------------------------------------------------- the trail


def test_the_audit_page_returns_newest_first(client: TestClient) -> None:
    client.patch(f"/groups/{GROUP_ID}/config", json={"sfw": False}, headers=_auth())
    client.put(f"/groups/{GROUP_ID}/rules", json={"body": "hi"}, headers=_auth())
    response = client.get(f"/groups/{GROUP_ID}/audit", headers=_auth())
    assert response.status_code == 200
    actions = [event["action"] for event in response.json()["events"]]
    assert actions == [audit.RULES_UPDATED, audit.CONFIG_UPDATED]


def test_the_audit_page_can_be_filtered_by_action(client: TestClient) -> None:
    client.patch(f"/groups/{GROUP_ID}/config", json={"sfw": False}, headers=_auth())
    client.put(f"/groups/{GROUP_ID}/rules", json={"body": "hi"}, headers=_auth())
    response = client.get(
        f"/groups/{GROUP_ID}/audit", params={"action": audit.CONFIG_UPDATED}, headers=_auth()
    )
    assert [e["action"] for e in response.json()["events"]] == [audit.CONFIG_UPDATED]


def test_a_full_page_carries_a_cursor(client: TestClient) -> None:
    client.patch(f"/groups/{GROUP_ID}/config", json={"sfw": False}, headers=_auth())
    client.put(f"/groups/{GROUP_ID}/rules", json={"body": "hi"}, headers=_auth())
    body = client.get(f"/groups/{GROUP_ID}/audit", params={"limit": 2}, headers=_auth()).json()
    assert body["next_before"] == body["events"][-1]["id"]

    short = client.get(f"/groups/{GROUP_ID}/audit", params={"limit": 50}, headers=_auth()).json()
    assert short["next_before"] is None


# ------------------------------------------------------------------- /me


def test_me_lists_the_groups_you_administer(client: TestClient) -> None:
    response = client.get("/me", headers=_auth())
    assert response.status_code == 200
    body = response.json()
    assert body["user_id"] == ADMIN_ID
    assert body["scopes"] == ["audit:read", "groups:read", "groups:write"]
    assert [g["group_id"] for g in body["groups"]] == [GROUP_ID]


def test_me_is_empty_for_someone_who_administers_nothing(client: TestClient) -> None:
    body = client.get("/me", headers=_auth(STRANGER_ID)).json()
    assert body["groups"] == []
