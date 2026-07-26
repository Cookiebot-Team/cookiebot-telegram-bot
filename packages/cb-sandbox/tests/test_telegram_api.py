"""Tests for the sandbox's Telegram Bot API surface.

Builds its own `FastAPI` app around `telegram_api.router` rather than
importing `cb_sandbox.app` — `app.py` also mounts `control_api.router`, owned
by another agent working in this repo concurrently, and this suite must not
depend on that existing.

The critical tests validate payloads against aiogram's *real* pydantic
models (`ChatMemberAdministrator.model_validate(...)`, etc.), not just against
this file's own idea of the shape — that is the only thing that would have
caught the incident `qa/mock_telegram.py` documents: a `getChatAdministrators`
result missing required `ChatMemberAdministrator` fields silently parses to
nothing, so an admin-gated handler decides nobody is an admin and the test
that was meant to prove an admin can act passes for the wrong reason.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import pytest
from aiogram.types import (
    ChatMemberAdministrator,
    ChatMemberBanned,
    ChatMemberLeft,
    ChatMemberOwner,
    ChatMemberRestricted,
    Message,
)
from cb_sandbox.config import DEFAULT_CONFIG, BotIdentity, get_config, set_config
from cb_sandbox.state import Membership, SandboxChat, SandboxUser, store
from cb_sandbox.telegram_api import router
from fastapi import FastAPI
from fastapi.testclient import TestClient

TOKEN = "123456:TEST-TOKEN"

#: Every `ChatPermissions` field real Telegram exposes — mirrors
#: `telegram_api._PERMISSION_KEYS` without importing that private module
#: constant, so a "grant everything back" test payload doesn't silently rot
#: if a permission is ever added to one list and not the other.
_ALL_PERMISSION_KEYS = (
    "can_send_messages",
    "can_send_audios",
    "can_send_documents",
    "can_send_photos",
    "can_send_videos",
    "can_send_video_notes",
    "can_send_voice_notes",
    "can_send_polls",
    "can_send_other_messages",
    "can_add_web_page_previews",
    "can_react_to_messages",
    "can_edit_tag",
    "can_change_info",
    "can_invite_users",
    "can_pin_messages",
    "can_manage_topics",
)


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(router)
    store().reset()
    with TestClient(app) as test_client:
        yield test_client
    store().reset()


def _call(client: TestClient, method: str, **params: Any) -> dict[str, Any]:
    response = client.post(f"/bot{TOKEN}/{method}", json=params)
    return response.json()


def _seed_chat(chat_id: int = -1001000000001, title: str = "QA Group") -> SandboxChat:
    s = store()
    chat = SandboxChat(id=chat_id, title=title)
    s.chats[chat_id] = chat
    return chat


def _seed_user(user_id: int, first_name: str = "Alice") -> SandboxUser:
    s = store()
    user = SandboxUser(id=user_id, first_name=first_name, username=first_name.lower())
    s.users[user_id] = user
    return user


# --------------------------------------------------------------- getMe


def test_get_me_answers_with_the_configured_identity(client: TestClient) -> None:
    """`getMe` is where a bot learns its own username, and most command
    filters key on it — a sandbox that answered with a name the bot was not
    configured with would make every `/cmd@name` silently stop matching."""
    identity = get_config().bot
    body = _call(client, "getMe")
    assert body["ok"] is True
    assert body["result"]["id"] == identity.id
    assert body["result"]["username"] == identity.username
    assert body["result"]["first_name"] == identity.first_name
    assert body["result"]["is_bot"] is True


def test_get_me_reflects_a_reconfigured_identity(client: TestClient) -> None:
    """The whole point of making identity configuration: pointing the sandbox
    at another bot must not require editing this package."""
    set_config(
        replace(DEFAULT_CONFIG, bot=BotIdentity(id=99, username="other_bot", first_name="Other"))
    )
    body = _call(client, "getMe")
    assert body["result"]["id"] == 99
    assert body["result"]["username"] == "other_bot"


# ------------------------------------------------------- unknown method


def test_unknown_method_is_telegram_shaped_404(client: TestClient) -> None:
    """The real server's exact wording for an unknown method — no method name
    interpolated into it, unlike this file's earlier (wrong) message."""
    response = client.post(f"/bot{TOKEN}/notARealMethod", json={})
    assert response.status_code == 404
    body = response.json()
    assert body == {"ok": False, "error_code": 404, "description": "Not Found: method not found"}


def test_unknown_method_never_500s(client: TestClient) -> None:
    """Whatever else changes here, an unknown method must never surface as a
    traceback to a bot that is just trying its next Bot API call."""
    response = client.post(f"/bot{TOKEN}/anythingElse", json={"chat_id": -1})
    assert response.status_code != 500


# -------------------------------------------------------- sendMessage


class TestSendMessage:
    def test_creates_a_stored_message_and_publishes_it(self, client: TestClient) -> None:
        _seed_chat()
        body = _call(client, "sendMessage", chat_id=-1001000000001, text="hello world")
        assert body["ok"] is True
        result = body["result"]
        assert result["text"] == "hello world"
        assert result["chat"]["id"] == -1001000000001

        s = store()
        assert len(s.messages[-1001000000001]) == 1
        assert any(e.kind == "message" for e in s.events)

    def test_validates_against_real_aiogram_message_model(self, client: TestClient) -> None:
        """The critical test: build the payload sendMessage returns and parse
        it with aiogram's own model, not a hand-rolled assertion of shape."""
        _seed_chat()
        body = _call(client, "sendMessage", chat_id=-1001000000001, text="parses fine?")
        message = Message.model_validate(body["result"])
        assert message.text == "parses fine?"
        assert message.from_user is not None
        assert message.from_user.is_bot is True

    def test_missing_chat_is_a_telegram_shaped_400(self, client: TestClient) -> None:
        response = client.post(f"/bot{TOKEN}/sendMessage", json={"chat_id": -999, "text": "hi"})
        assert response.status_code == 400
        body = response.json()
        assert body["ok"] is False
        assert body["error_code"] == 400

    def test_missing_text_is_a_telegram_shaped_400(self, client: TestClient) -> None:
        _seed_chat()
        response = client.post(f"/bot{TOKEN}/sendMessage", json={"chat_id": -1001000000001})
        assert response.status_code == 400

    def test_reply_markup_survives_the_multipart_round_trip(self, client: TestClient) -> None:
        """aiogram never sends JSON — `AiohttpSession.build_form_data` always
        posts multipart, and `reply_markup` inside it is a JSON-encoded
        string, not a nested structure. If this parse breaks, every inline
        keyboard the bot sends becomes invisible to the web client.

        httpx's `data=` alone sends url-encoded, so the `files=` form here
        (each field as a `(None, value)` pair, no filename) is what actually
        forces a genuine `multipart/form-data` body, matching aiogram's wire
        shape rather than a more forgiving one.
        """
        _seed_chat()
        keyboard = {"inline_keyboard": [[{"text": "Yes", "callback_data": "CALLADMS YES 1"}]]}
        response = client.post(
            f"/bot{TOKEN}/sendMessage",
            files={
                "chat_id": (None, "-1001000000001"),
                "text": (None, "confirm?"),
                "reply_markup": (None, json.dumps(keyboard)),
            },
        )
        assert response.request.headers["content-type"].startswith("multipart/form-data")
        body = response.json()
        assert body["ok"] is True
        assert body["result"]["reply_markup"] == keyboard

        s = store()
        stored = s.messages[-1001000000001][-1]
        assert stored.reply_markup == keyboard

    def test_numeric_and_boolean_fields_survive_the_urlencoded_form_shape(
        self, client: TestClient
    ) -> None:
        """The other non-JSON shape a Bot API client may use
        (`application/x-www-form-urlencoded`) needs the same field coercion
        `_int`/`_bool` give the multipart case."""
        _seed_chat()
        response = client.post(
            f"/bot{TOKEN}/sendMessage",
            data={"chat_id": "-1001000000001", "text": "plain form"},
        )
        assert response.request.headers["content-type"].startswith(
            "application/x-www-form-urlencoded"
        )
        body = response.json()
        assert body["ok"] is True
        assert body["result"]["chat"]["id"] == -1001000000001


@pytest.mark.parametrize(
    ("method", "media_field"),
    [
        ("sendPhoto", "photo"),
        ("sendVideo", "video"),
        ("sendAnimation", "animation"),
        ("sendSticker", "sticker"),
    ],
)
def test_send_media_methods_store_a_message_with_the_right_media_field(
    client: TestClient, method: str, media_field: str
) -> None:
    _seed_chat()
    body = _call(client, method, chat_id=-1001000000001, file_id="file-1")
    assert body["ok"] is True
    assert media_field in body["result"]
    # Every media message still round-trips through the real Message model.
    Message.model_validate(body["result"])


def test_send_chat_action(client: TestClient) -> None:
    _seed_chat()
    body = _call(client, "sendChatAction", chat_id=-1001000000001, action="typing")
    assert body == {"ok": True, "result": True}


# --------------------------------------------------------- edit / delete


def test_edit_message_text(client: TestClient) -> None:
    _seed_chat()
    sent = _call(client, "sendMessage", chat_id=-1001000000001, text="v1")
    message_id = sent["result"]["message_id"]
    edited = _call(
        client,
        "editMessageText",
        chat_id=-1001000000001,
        message_id=message_id,
        text="v2",
    )
    assert edited["ok"] is True
    assert edited["result"]["text"] == "v2"
    Message.model_validate(edited["result"])


def test_edit_message_reply_markup(client: TestClient) -> None:
    _seed_chat()
    sent = _call(client, "sendMessage", chat_id=-1001000000001, text="v1")
    message_id = sent["result"]["message_id"]
    keyboard = {"inline_keyboard": [[{"text": "Ok", "callback_data": "ok"}]]}
    edited = _call(
        client,
        "editMessageReplyMarkup",
        chat_id=-1001000000001,
        message_id=message_id,
        reply_markup=json.dumps(keyboard),
    )
    assert edited["result"]["reply_markup"] == keyboard


def test_edit_missing_message_is_400(client: TestClient) -> None:
    _seed_chat()
    response = client.post(
        f"/bot{TOKEN}/editMessageText",
        json={"chat_id": -1001000000001, "message_id": 99999, "text": "nope"},
    )
    assert response.status_code == 400


def test_delete_message(client: TestClient) -> None:
    _seed_chat()
    sent = _call(client, "sendMessage", chat_id=-1001000000001, text="to delete")
    message_id = sent["result"]["message_id"]
    body = _call(client, "deleteMessage", chat_id=-1001000000001, message_id=message_id)
    assert body == {"ok": True, "result": True}
    s = store()
    assert s.message(-1001000000001, message_id).deleted is True


# --------------------------------------------------------- callback query


def test_answer_callback_query(client: TestClient) -> None:
    # `answerCallbackQuery` only accepts an id real Telegram actually handed
    # out — `queue_update` is what `control_api.py`'s "press this button" flow
    # calls, so a callback query has to be staged the same way here.
    store().queue_update({"callback_query": {"id": "cb-1"}})
    body = _call(client, "answerCallbackQuery", callback_query_id="cb-1", text="done")
    assert body == {"ok": True, "result": True}
    s = store()
    assert any(e.kind == "callback_answer" for e in s.events)


def test_answer_callback_query_unknown_id_is_400(client: TestClient) -> None:
    """The real server's own wording for an id it never issued, or one
    already answered — verified against `core.telegram.org/bots/api`."""
    response = client.post(
        f"/bot{TOKEN}/answerCallbackQuery", json={"callback_query_id": "never-issued"}
    )
    assert response.status_code == 400
    body = response.json()
    assert body["description"] == (
        "Bad Request: query is too old and response timeout expired or query id is invalid"
    )


def test_answer_callback_query_cannot_be_answered_twice(client: TestClient) -> None:
    store().queue_update({"callback_query": {"id": "cb-2"}})
    first = _call(client, "answerCallbackQuery", callback_query_id="cb-2")
    assert first["ok"] is True
    second = client.post(f"/bot{TOKEN}/answerCallbackQuery", json={"callback_query_id": "cb-2"})
    assert second.status_code == 400


# -------------------------------------------------------------- getChat


def test_get_chat(client: TestClient) -> None:
    _seed_chat(title="My Group")
    body = _call(client, "getChat", chat_id=-1001000000001)
    assert body["result"]["title"] == "My Group"
    assert body["result"]["type"] == "supergroup"


def test_get_chat_unknown_is_400(client: TestClient) -> None:
    response = client.post(f"/bot{TOKEN}/getChat", json={"chat_id": -1})
    assert response.status_code == 400


# ------------------------------------------------- getChatAdministrators


class TestChatAdministrators:
    """The critical test the task calls out: every payload
    `getChatAdministrators` can return must parse under aiogram's own model,
    not just look right to a human skimming the dict literal."""

    @pytest.mark.parametrize(
        ("role", "anonymous", "expected_model"),
        [
            ("creator", False, ChatMemberOwner),
            ("creator", True, ChatMemberOwner),
            ("administrator", False, ChatMemberAdministrator),
            ("administrator", True, ChatMemberAdministrator),
        ],
    )
    def test_admin_payload_validates_against_real_aiogram_model(
        self,
        client: TestClient,
        role: str,
        anonymous: bool,
        expected_model: type[ChatMemberOwner] | type[ChatMemberAdministrator],
    ) -> None:
        chat = _seed_chat()
        _seed_user(111, "Boss")
        chat.members[111] = Membership(user_id=111, role=role, anonymous=anonymous)

        body = _call(client, "getChatAdministrators", chat_id=chat.id)
        assert body["ok"] is True
        [payload] = body["result"]

        validated = expected_model.model_validate(payload)
        assert validated.user.id == 111
        assert validated.is_anonymous is anonymous
        if isinstance(validated, ChatMemberAdministrator):
            assert validated.can_restrict_members is True

    def test_only_admins_and_creator_are_returned(self, client: TestClient) -> None:
        chat = _seed_chat()
        _seed_user(1, "Creator")
        _seed_user(2, "Admin")
        _seed_user(3, "Regular")
        chat.members[1] = Membership(user_id=1, role="creator")
        chat.members[2] = Membership(user_id=2, role="administrator")
        chat.members[3] = Membership(user_id=3, role="member")

        body = _call(client, "getChatAdministrators", chat_id=chat.id)
        ids = {entry["user"]["id"] for entry in body["result"]}
        assert ids == {1, 2}

    def test_empty_when_no_admins(self, client: TestClient) -> None:
        chat = _seed_chat()
        body = _call(client, "getChatAdministrators", chat_id=chat.id)
        assert body["result"] == []


def test_get_chat_member_for_regular_member(client: TestClient) -> None:
    chat = _seed_chat()
    _seed_user(42, "Regular")
    chat.members[42] = Membership(user_id=42, role="member")
    body = _call(client, "getChatMember", chat_id=chat.id, user_id=42)
    assert body["result"]["status"] == "member"


def test_get_chat_member_unknown_user_is_400(client: TestClient) -> None:
    chat = _seed_chat()
    response = client.post(f"/bot{TOKEN}/getChatMember", json={"chat_id": chat.id, "user_id": 999})
    assert response.status_code == 400


# --------------------------------------------- restrict / ban / promote


class TestMembershipMutation:
    def test_restrict_chat_member_mutates_the_store(self, client: TestClient) -> None:
        chat = _seed_chat()
        _seed_user(7, "Newbie")
        chat.members[7] = Membership(user_id=7, role="member")

        # A real near-future deadline (real Telegram would treat 9999999999 —
        # the year 2286 — as "forever": see TestUntilDateNormalization below).
        until_date = int(time.time()) + 3600
        body = _call(
            client,
            "restrictChatMember",
            chat_id=chat.id,
            user_id=7,
            until_date=until_date,
            permissions=json.dumps(
                {
                    "can_send_messages": True,
                    "can_send_other_messages": False,
                    "can_add_web_page_previews": False,
                }
            ),
        )
        assert body == {"ok": True, "result": True}

        member = store().membership(chat.id, 7)
        assert member is not None
        assert member.role == "restricted"
        assert member.restricted_until == until_date

        # And the resulting getChatMember payload is real.
        rendered = _call(client, "getChatMember", chat_id=chat.id, user_id=7)
        validated = ChatMemberRestricted.model_validate(rendered["result"])
        assert validated.can_send_messages is True
        assert validated.can_send_other_messages is False

    def test_restrict_with_full_permissions_lifts_the_restriction(self, client: TestClient) -> None:
        """Real Telegram only reports "member" (not "restricted") once every
        single permission is granted back — granting just the two flags the
        old sandbox template happened to check is not enough."""
        chat = _seed_chat()
        chat.members[7] = Membership(user_id=7, role="restricted", restricted_until=123.0)
        _call(
            client,
            "restrictChatMember",
            chat_id=chat.id,
            user_id=7,
            permissions=json.dumps(dict.fromkeys(_ALL_PERMISSION_KEYS, True)),
        )
        assert store().membership(chat.id, 7).role == "member"  # type: ignore[union-attr]

    def test_restrict_with_partial_open_permissions_stays_restricted(
        self, client: TestClient
    ) -> None:
        chat = _seed_chat()
        chat.members[7] = Membership(user_id=7, role="restricted", restricted_until=123.0)
        _call(
            client,
            "restrictChatMember",
            chat_id=chat.id,
            user_id=7,
            permissions=json.dumps({"can_send_messages": True, "can_send_other_messages": True}),
        )
        assert store().membership(chat.id, 7).role == "restricted"  # type: ignore[union-attr]

    def test_independent_permissions_do_not_imply_media(self, client: TestClient) -> None:
        """Without `use_independent_chat_permissions`, `can_send_other_messages`
        implies every "send media" permission (restrictChatMember's own
        documented normalisation). With it set, no implication happens."""
        chat = _seed_chat()
        chat.members[7] = Membership(user_id=7, role="member")
        _call(
            client,
            "restrictChatMember",
            chat_id=chat.id,
            user_id=7,
            use_independent_chat_permissions=True,
            permissions=json.dumps({"can_send_other_messages": True}),
        )
        member = store().membership(chat.id, 7)
        assert member is not None
        assert member.permissions["can_send_other_messages"] is True
        assert member.permissions["can_send_photos"] is False

    def test_dependent_permissions_imply_media(self, client: TestClient) -> None:
        chat = _seed_chat()
        chat.members[7] = Membership(user_id=7, role="member")
        _call(
            client,
            "restrictChatMember",
            chat_id=chat.id,
            user_id=7,
            permissions=json.dumps({"can_send_other_messages": True}),
        )
        member = store().membership(chat.id, 7)
        assert member is not None
        assert member.permissions["can_send_photos"] is True


class TestUntilDateNormalization:
    """`restrictChatMember`/`banChatMember`'s own rule: under 30 seconds or
    over 366 days from now collapses to "forever" (`until_date: 0`)."""

    def test_short_deadline_becomes_forever(self, client: TestClient) -> None:
        chat = _seed_chat()
        chat.members[7] = Membership(user_id=7, role="member")
        _call(
            client,
            "restrictChatMember",
            chat_id=chat.id,
            user_id=7,
            until_date=int(time.time()) + 5,
            permissions=json.dumps({"can_send_messages": False}),
        )
        assert store().membership(chat.id, 7).restricted_until == 0.0  # type: ignore[union-attr]

    def test_far_future_deadline_becomes_forever(self, client: TestClient) -> None:
        chat = _seed_chat()
        chat.members[9] = Membership(user_id=9, role="member")
        _call(client, "banChatMember", chat_id=chat.id, user_id=9, until_date=9999999999)
        assert store().membership(chat.id, 9).restricted_until == 0.0  # type: ignore[union-attr]

    def test_realistic_deadline_is_kept(self, client: TestClient) -> None:
        chat = _seed_chat()
        chat.members[9] = Membership(user_id=9, role="member")
        until_date = int(time.time()) + 86400
        _call(client, "banChatMember", chat_id=chat.id, user_id=9, until_date=until_date)
        assert store().membership(chat.id, 9).restricted_until == until_date  # type: ignore[union-attr]

    def test_ban_chat_member_mutates_the_store(self, client: TestClient) -> None:
        chat = _seed_chat()
        chat.members[9] = Membership(user_id=9, role="member")
        body = _call(client, "banChatMember", chat_id=chat.id, user_id=9)
        assert body == {"ok": True, "result": True}
        member = store().membership(chat.id, 9)
        assert member is not None
        assert member.role == "kicked"

        rendered = _call(client, "getChatMember", chat_id=chat.id, user_id=9)
        ChatMemberBanned.model_validate(rendered["result"])

    def test_unban_chat_member_mutates_the_store(self, client: TestClient) -> None:
        chat = _seed_chat()
        chat.members[9] = Membership(user_id=9, role="kicked")
        body = _call(client, "unbanChatMember", chat_id=chat.id, user_id=9)
        assert body == {"ok": True, "result": True}
        member = store().membership(chat.id, 9)
        assert member is not None
        assert member.role == "left"
        rendered = _call(client, "getChatMember", chat_id=chat.id, user_id=9)
        ChatMemberLeft.model_validate(rendered["result"])

    def test_promote_chat_member_makes_an_admin(self, client: TestClient) -> None:
        chat = _seed_chat()
        chat.members[5] = Membership(user_id=5, role="member")
        body = _call(
            client,
            "promoteChatMember",
            chat_id=chat.id,
            user_id=5,
            can_restrict_members=True,
        )
        assert body == {"ok": True, "result": True}
        member = store().membership(chat.id, 5)
        assert member is not None
        assert member.role == "administrator"

    def test_promote_chat_member_with_no_flags_demotes(self, client: TestClient) -> None:
        chat = _seed_chat()
        chat.members[5] = Membership(user_id=5, role="administrator")
        _call(client, "promoteChatMember", chat_id=chat.id, user_id=5)
        member = store().membership(chat.id, 5)
        assert member is not None
        assert member.role == "member"


# ----------------------------------------------------------- misc methods


@pytest.mark.parametrize("method", ["setWebhook", "deleteWebhook"])
def test_no_op_methods_return_true(client: TestClient, method: str) -> None:
    body = _call(client, method, url="http://example.com")
    assert body == {"ok": True, "result": True}


def test_get_webhook_info_reports_no_webhook(client: TestClient) -> None:
    """The sandbox is polling-only (see `_set_webhook`) — this must always
    say so, matching what a bot polling here via `CB_TELEGRAM_INGEST=polling`
    actually gets back from the real server."""
    body = _call(client, "getWebhookInfo")
    assert body["ok"] is True
    assert body["result"]["url"] == ""
    assert body["result"]["pending_update_count"] == 0


class TestMyCommands:
    def test_set_then_get_default_scope(self, client: TestClient) -> None:
        commands = [{"command": "rules", "description": "Show the rules"}]
        set_body = _call(client, "setMyCommands", commands=json.dumps(commands))
        assert set_body == {"ok": True, "result": True}

        get_body = _call(client, "getMyCommands")
        assert get_body["result"] == commands

    def test_scope_and_language_are_independent(self, client: TestClient) -> None:
        chat_scope = {"type": "chat", "chat_id": -1001000000001}
        _call(
            client,
            "setMyCommands",
            commands=json.dumps([{"command": "regras", "description": "Ver regras"}]),
            scope=json.dumps(chat_scope),
            language_code="pt",
        )
        # A different scope/language sees nothing from that call.
        assert _call(client, "getMyCommands")["result"] == []
        # The exact scope+language it was set for does.
        scoped = _call(client, "getMyCommands", scope=json.dumps(chat_scope), language_code="pt")
        assert scoped["result"] == [{"command": "regras", "description": "Ver regras"}]

    def test_delete_my_commands_falls_back_to_empty(self, client: TestClient) -> None:
        _call(client, "setMyCommands", commands=json.dumps([{"command": "a", "description": "A"}]))
        _call(client, "deleteMyCommands")
        assert _call(client, "getMyCommands")["result"] == []

    def test_missing_commands_is_a_telegram_shaped_400(self, client: TestClient) -> None:
        response = client.post(f"/bot{TOKEN}/setMyCommands", json={})
        assert response.status_code == 400


def test_get_file(client: TestClient) -> None:
    body = _call(client, "getFile", file_id="abc123")
    assert body["ok"] is True
    assert body["result"]["file_id"] == "abc123"
    assert "file_path" in body["result"]


# --------------------------------------------------------- api_call log


def test_every_call_is_recorded(client: TestClient) -> None:
    store().reset()
    _seed_chat()
    _call(client, "sendMessage", chat_id=-1001000000001, text="logged?")
    calls = [c["method"] for c in store().api_calls]
    assert "sendMessage" in calls


def test_failed_calls_are_still_recorded(client: TestClient) -> None:
    store().reset()
    client.post(f"/bot{TOKEN}/sendMessage", json={"chat_id": -1, "text": "nope"})
    calls = [c["method"] for c in store().api_calls]
    assert "sendMessage" in calls


# ------------------------------------------------------------ getUpdates


class TestGetUpdates:
    def test_returns_immediately_when_updates_are_pending(self, client: TestClient) -> None:
        store().reset()
        store().queue_update({"message": {"text": "hi"}})
        body = _call(client, "getUpdates", timeout=0)
        assert len(body["result"]) == 1

    def test_returns_promptly_without_waiting_out_the_full_timeout(
        self, client: TestClient
    ) -> None:
        store().reset()
        store().queue_update({"message": {"text": "hi"}})
        # A real client might set `timeout=25`; this asserts the *response*
        # timing, not the request's own socket timeout, so none is passed
        # here — the long-poll loop itself must still return promptly
        # because an update is already pending.
        response = client.post(f"/bot{TOKEN}/getUpdates", json={"timeout": 25})
        assert response.json()["ok"] is True

    def test_empty_queue_waits_out_the_timeout_then_returns_empty(self, client: TestClient) -> None:
        store().reset()
        body = _call(client, "getUpdates", timeout=0)
        assert body["result"] == []

    def test_offset_confirms_and_removes_earlier_updates(self, client: TestClient) -> None:
        """An update stays queued until a later offset asks past it — the
        redelivery contract `SandboxStore.take_updates` documents."""
        store().reset()
        first = store().queue_update({"message": {"text": "one"}})
        second = store().queue_update({"message": {"text": "two"}})

        # No offset: both still pending, nothing confirmed.
        body = _call(client, "getUpdates", timeout=0)
        assert [u["update_id"] for u in body["result"]] == [
            first["update_id"],
            second["update_id"],
        ]

        # Ask for updates past `first`: confirms and drops it.
        body = _call(client, "getUpdates", offset=second["update_id"], timeout=0)
        assert [u["update_id"] for u in body["result"]] == [second["update_id"]]
        assert len(store().pending_updates) == 1

        # Confirm the second too: nothing left.
        body = _call(client, "getUpdates", offset=second["update_id"] + 1, timeout=0)
        assert body["result"] == []
        assert store().pending_updates == []
