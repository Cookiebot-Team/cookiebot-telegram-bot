"""Tests for the sandbox control plane.

Builds a bare FastAPI app around just `control_api.router` rather than
`cb_sandbox.app` — that module also mounts `telegram_api.py`, which is another
agent's concern and not needed to exercise the control plane in isolation.

The shape assertion that matters most: every queued Telegram update must
validate against aiogram's real `Update` model, because a malformed update is
exactly the failure mode this sandbox exists to catch (the gateway silently
ignores an update it cannot parse).
"""

from __future__ import annotations

import time
from dataclasses import replace
from typing import Any

import pytest
from aiogram.types import Update
from cb_sandbox.config import (
    DEFAULT_CONFIG,
    SeedChat,
    SeedFixture,
    SeedMember,
    SeedUser,
    get_config,
    set_config,
)
from cb_sandbox.control_api import ANONYMOUS_BOT_ID, router
from cb_sandbox.state import SandboxMessage, store
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def client() -> TestClient:
    store().reset()
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _seed(client: TestClient, scenario: str = "default") -> dict[str, Any]:
    resp = client.post("/api/seed", json={"scenario": scenario})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _user(snapshot: dict[str, Any], username: str) -> dict[str, Any]:
    return next(u for u in snapshot["users"] if u["username"] == username)


def _default_chat_id(snapshot: dict[str, Any]) -> int:
    return int(snapshot["chats"][0]["id"])


def _last_update() -> dict[str, Any]:
    pending = store().pending_updates
    assert pending, "expected a queued update"
    return pending[-1]


# --------------------------------------------------------------------- seeding


class TestSeed:
    @pytest.mark.parametrize("scenario", ["default", "empty", "dm"])
    def test_known_scenarios_seed_cleanly(self, client: TestClient, scenario: str) -> None:
        resp = client.post("/api/seed", json={"scenario": scenario})
        assert resp.status_code == 200, resp.text

    def test_seed_without_a_name_applies_the_configured_default(self, client: TestClient) -> None:
        """A caller that just wants "put something in front of me" should not
        have to know which fixture this particular bot calls its default."""
        resp = client.post("/api/seed", json={})
        assert resp.status_code == 200, resp.text
        assert len(resp.json()["chats"]) == 1

    def test_unknown_scenario_is_a_4xx_not_a_500(self, client: TestClient) -> None:
        resp = client.post("/api/seed", json={"scenario": "not-a-real-scenario"})
        assert 400 <= resp.status_code < 500

    def test_default_seed_leaves_the_bot_as_administrator(self, client: TestClient) -> None:
        snapshot = _seed(client, "default")
        bot_id = get_config().bot.id
        assert snapshot["bot"] is not None
        assert snapshot["bot"]["id"] == bot_id
        chat = snapshot["chats"][0]
        bot_membership = next(m for m in chat["members"] if m["user_id"] == bot_id)
        assert bot_membership["role"] == "administrator"
        # A creator, a plain member, and an admin with anonymity on — the
        # scenario the human can immediately exercise every feature against.
        roles = {m["role"] for m in chat["members"]}
        assert "creator" in roles
        anon_admins = [m for m in chat["members"] if m["anonymous"]]
        assert anon_admins and anon_admins[0]["role"] == "administrator"

    def test_empty_seed_has_no_bot_and_no_chats(self, client: TestClient) -> None:
        snapshot = _seed(client, "empty")
        assert snapshot["bot"] is None
        assert snapshot["chats"] == []
        assert snapshot["users"] == []

    def test_dm_seed_is_a_private_chat(self, client: TestClient) -> None:
        snapshot = _seed(client, "dm")
        assert snapshot["chats"][0]["type"] == "private"

    def test_a_configured_seed_can_leave_a_user_out_of_every_chat(self, client: TestClient) -> None:
        """The shape every join-time check needs in front of it: an account
        that exists but has joined nothing, so that pressing "join" is the
        whole test rather than something the seed already did off-screen."""
        set_config(
            replace(
                DEFAULT_CONFIG,
                seeds=(
                    SeedFixture(
                        name="raid",
                        users=(
                            SeedUser(key="ana", first_name="Ana", username="ana"),
                            SeedUser(key="raider", first_name="Raider", username="raider"),
                        ),
                        chats=(
                            SeedChat(
                                key="main",
                                title="Raid target",
                                members=(SeedMember(user="ana", role="creator"),),
                            ),
                        ),
                    ),
                ),
                default_seed="raid",
            )
        )
        snapshot = _seed(client, "raid")
        assert len(snapshot["chats"]) == 1
        member_ids = {m["user_id"] for m in snapshot["chats"][0]["members"]}
        assert _user(snapshot, "raider")["id"] not in member_ids
        assert _user(snapshot, "ana")["id"] in member_ids

    def test_a_seed_naming_an_undeclared_member_still_builds_the_rest(
        self, client: TestClient
    ) -> None:
        """A typo in one member line is a config bug, not a reason to leave
        the tester with no world at all."""
        set_config(
            replace(
                DEFAULT_CONFIG,
                seeds=(
                    SeedFixture(
                        name="typo",
                        users=(SeedUser(key="ana", first_name="Ana", username="ana"),),
                        chats=(
                            SeedChat(
                                key="main",
                                title="Group",
                                members=(
                                    SeedMember(user="ana", role="creator"),
                                    SeedMember(user="nobody"),
                                ),
                            ),
                        ),
                    ),
                ),
                default_seed="typo",
            )
        )
        snapshot = _seed(client, "typo")
        member_ids = {m["user_id"] for m in snapshot["chats"][0]["members"]}
        assert _user(snapshot, "ana")["id"] in member_ids

    def test_reset_reseeds_default(self, client: TestClient) -> None:
        _seed(client, "empty")
        resp = client.post("/api/reset")
        assert resp.status_code == 200
        snapshot = resp.json()
        assert snapshot["bot"] is not None
        assert len(snapshot["chats"]) == 1


# --------------------------------------------------------------------- state shape


class TestStateShape:
    def test_state_matches_web_types_field_for_field(self, client: TestClient) -> None:
        _seed(client, "default")
        snapshot = client.get("/api/state").json()
        assert set(snapshot.keys()) == {
            "users",
            "chats",
            "messages",
            "api_calls",
            "bot",
            "scenarios",
            "active_scenario_id",
            "features",
        }

        user = snapshot["users"][0]
        assert set(user.keys()) == {
            "id",
            "first_name",
            "last_name",
            "username",
            "language_code",
            "is_bot",
        }

        chat = snapshot["chats"][0]
        assert set(chat.keys()) == {"id", "title", "type", "members"}

        membership = chat["members"][0]
        assert set(membership.keys()) == {
            "user_id",
            "role",
            "anonymous",
            "joined_at",
            "restricted_until",
        }

        assert snapshot["bot"] is not None
        assert set(snapshot["bot"].keys()) == set(user.keys())

    def test_message_shape_matches_web_types(self, client: TestClient) -> None:
        snapshot = _seed(client, "default")
        chat_id = _default_chat_id(snapshot)
        bob = _user(snapshot, "bob")
        client.post(f"/api/chats/{chat_id}/messages", json={"user_id": bob["id"], "text": "/rules"})

        state = client.get("/api/state").json()
        message = state["messages"][str(chat_id)][0]
        assert set(message.keys()) == {
            "message_id",
            "chat_id",
            "from_id",
            "text",
            "date",
            "sender_chat_id",
            "reply_to_message_id",
            "reply_markup",
            "media",
            "media_file_id",
            "media_caption",
            "entities",
            "caption_entities",
            "service",
            "edited",
            "deleted",
            "scenario_id",
        }
        # Both entity lists are part of the web-facing shape on purpose: since
        # `telegram_api` started parsing `parse_mode` markup the way the real
        # Bot API does, `text` is the *plain* string and the formatting the bot
        # asked for lives only here. Drop them and the client silently renders
        # every bot message stripped of its links and emphasis.
        assert message["entities"] == [{"type": "bot_command", "offset": 0, "length": 6}]
        assert message["caption_entities"] == []


# --------------------------------------------------------------------- sending messages


class TestSendMessage:
    def test_send_message_queues_a_valid_update(self, client: TestClient) -> None:
        snapshot = _seed(client, "default")
        chat_id = _default_chat_id(snapshot)
        bob = _user(snapshot, "bob")

        resp = client.post(
            f"/api/chats/{chat_id}/messages", json={"user_id": bob["id"], "text": "hello world"}
        )
        assert resp.status_code == 201, resp.text

        update = _last_update()
        Update.model_validate(update)
        assert update["message"]["text"] == "hello world"
        assert update["message"]["from"]["id"] == bob["id"]
        assert update["message"]["chat"]["id"] == chat_id

    @pytest.mark.parametrize(
        ("text", "expected_length"),
        [
            ("/start", len("/start")),
            ("/start@some_bot arg", len("/start@some_bot")),
            ("/newrules some rules here", len("/newrules")),
        ],
    )
    def test_command_text_gets_a_bot_command_entity(
        self, client: TestClient, text: str, expected_length: int
    ) -> None:
        snapshot = _seed(client, "default")
        chat_id = _default_chat_id(snapshot)
        bob = _user(snapshot, "bob")

        resp = client.post(
            f"/api/chats/{chat_id}/messages", json={"user_id": bob["id"], "text": text}
        )
        assert resp.status_code == 201

        update = _last_update()
        Update.model_validate(update)
        entities = update["message"]["entities"]
        assert entities == [{"type": "bot_command", "offset": 0, "length": expected_length}]

    def test_plain_text_has_no_entities(self, client: TestClient) -> None:
        snapshot = _seed(client, "default")
        chat_id = _default_chat_id(snapshot)
        bob = _user(snapshot, "bob")

        client.post(
            f"/api/chats/{chat_id}/messages", json={"user_id": bob["id"], "text": "not a command"}
        )
        update = _last_update()
        assert update["message"]["entities"] == []

    def test_anonymous_send_reproduces_group_anonymous_bot(self, client: TestClient) -> None:
        snapshot = _seed(client, "default")
        chat_id = _default_chat_id(snapshot)
        carol = _user(snapshot, "carol")  # admin with anonymity toggled on by the default scenario

        resp = client.post(
            f"/api/chats/{chat_id}/messages",
            json={"user_id": carol["id"], "text": "shh", "anonymous": True},
        )
        assert resp.status_code == 201

        update = _last_update()
        Update.model_validate(update)
        message = update["message"]
        assert message["from"]["id"] == ANONYMOUS_BOT_ID
        assert message["from"]["username"] == "GroupAnonymousBot"
        assert message["from"]["is_bot"] is True
        assert message["sender_chat"]["id"] == chat_id

    def test_anonymous_send_requires_the_toggle(self, client: TestClient) -> None:
        snapshot = _seed(client, "default")
        chat_id = _default_chat_id(snapshot)
        bob = _user(snapshot, "bob")  # plain member, anonymity not toggled

        resp = client.post(
            f"/api/chats/{chat_id}/messages",
            json={"user_id": bob["id"], "text": "shh", "anonymous": True},
        )
        assert resp.status_code == 400

    def test_message_from_a_non_member_is_rejected(self, client: TestClient) -> None:
        snapshot = _seed(client, "default")
        chat_id = _default_chat_id(snapshot)
        outsider = client.post("/api/users", json={"first_name": "Eve", "username": "eve"}).json()

        resp = client.post(
            f"/api/chats/{chat_id}/messages", json={"user_id": outsider["id"], "text": "hi"}
        )
        assert resp.status_code == 400

    def test_message_needs_text_or_media(self, client: TestClient) -> None:
        snapshot = _seed(client, "default")
        chat_id = _default_chat_id(snapshot)
        bob = _user(snapshot, "bob")
        resp = client.post(f"/api/chats/{chat_id}/messages", json={"user_id": bob["id"]})
        assert resp.status_code == 400

    def test_unknown_chat_is_404(self, client: TestClient) -> None:
        resp = client.post("/api/chats/999999/messages", json={"user_id": 1, "text": "hi"})
        assert resp.status_code == 404


# --------------------------------------------------------------------- chat creation


class TestCreateChat:
    def test_default_chat_type_is_supergroup(self, client: TestClient) -> None:
        resp = client.post("/api/chats", json={"title": "Some Group"})
        assert resp.status_code == 201
        assert resp.json()["type"] == "supergroup"

    def test_can_create_a_private_chat_on_demand(self, client: TestClient) -> None:
        """A tester needs to open a private chat on demand, not only through
        the one hardcoded into the `dm` seed scenario, to drive any command
        gated on `chat.type == "private"`."""
        resp = client.post("/api/chats", json={"title": "Carol DM", "type": "private"})
        assert resp.status_code == 201
        assert resp.json()["type"] == "private"


class TestPrivateChat:
    """A DM's id must be the user's own id — that is how Telegram addresses one,
    and how every handler that answers privately (`bot.send_message(user_id, ...)`)
    reaches it. A DM allocated an id from the chat counter is unreachable by that
    code no matter what the tester does."""

    def test_dm_chat_id_is_the_users_own_id(self, client: TestClient) -> None:
        snapshot = _seed(client, "default")
        bob = _user(snapshot, "bob")

        chat = client.post(f"/api/users/{bob['id']}/dm").json()
        assert chat["id"] == bob["id"]
        assert chat["type"] == "private"

    def test_opening_a_dm_twice_returns_the_same_chat(self, client: TestClient) -> None:
        snapshot = _seed(client, "default")
        bob = _user(snapshot, "bob")

        first = client.post(f"/api/users/{bob['id']}/dm").json()
        second = client.post(f"/api/users/{bob['id']}/dm").json()
        assert first["id"] == second["id"]
        assert len(client.get("/api/state").json()["chats"]) == 2  # the group and the one DM

    def test_the_dm_seed_scenario_uses_the_same_rule(self, client: TestClient) -> None:
        snapshot = _seed(client, "dm")
        dana = _user(snapshot, "dana")
        assert [chat["id"] for chat in snapshot["chats"]] == [dana["id"]]

    def test_the_dm_is_addressable_as_a_chat(self, client: TestClient) -> None:
        """The half this file can assert: once opened, the DM is a chat the
        control plane can send into by the user's own id. The other half — that
        the *bot* is refused until it is opened — is a Bot API behaviour and
        lives in `test_bot_api_compat.py`, which mounts that router."""
        snapshot = _seed(client, "default")
        bob = _user(snapshot, "bob")
        client.post(f"/api/users/{bob['id']}/dm")

        resp = client.post(
            f"/api/chats/{bob['id']}/messages", json={"user_id": bob["id"], "text": "/privacy"}
        )
        assert resp.status_code == 201
        assert resp.json()["chat_id"] == bob["id"]


# --------------------------------------------------------------------- join / leave


class TestMembership:
    def test_self_join_queues_new_chat_members_from_the_joiner(self, client: TestClient) -> None:
        snapshot = _seed(client, "default")
        chat_id = _default_chat_id(snapshot)
        newcomer = client.post("/api/users", json={"first_name": "Eve", "username": "eve"}).json()

        resp = client.post(f"/api/chats/{chat_id}/join", json={"user_id": newcomer["id"]})
        assert resp.status_code == 200

        update = _last_update()
        Update.model_validate(update)
        message = update["message"]
        assert message["from"]["id"] == newcomer["id"]
        assert message["new_chat_members"][0]["id"] == newcomer["id"]

    def test_the_join_service_message_is_stored_and_can_be_replied_to(
        self, client: TestClient
    ) -> None:
        """The captcha answers a join with `message.reply(...)`, which sends
        `reply_to_message_id` pointing at the join's own service message. Queue
        the update without storing the message and every captcha issuance comes
        back `400 Bad Request: message to reply not found` — a bug that reads
        as "the captcha is broken" while the captcha is fine.
        """
        snapshot = _seed(client, "default")
        chat_id = _default_chat_id(snapshot)
        newcomer = client.post("/api/users", json={"first_name": "Eve", "username": "eve"}).json()

        client.post(f"/api/chats/{chat_id}/join", json={"user_id": newcomer["id"]})

        stored = client.get("/api/state").json()["messages"][str(chat_id)]
        assert len(stored) == 1
        join_message = stored[0]
        assert join_message["text"] is None
        assert join_message["service"] == {
            "kind": "join",
            "user_id": newcomer["id"],
            "by_user_id": None,
        }
        assert store().message(chat_id, join_message["message_id"]) is not None

    def test_the_leave_service_message_is_stored_too(self, client: TestClient) -> None:
        snapshot = _seed(client, "default")
        chat_id = _default_chat_id(snapshot)
        bob = _user(snapshot, "bob")

        client.post(f"/api/chats/{chat_id}/leave", json={"user_id": bob["id"]})

        stored = client.get("/api/state").json()["messages"][str(chat_id)]
        assert stored[-1]["service"] == {"kind": "leave", "user_id": bob["id"], "by_user_id": None}

    def test_added_by_another_queues_new_chat_members_from_the_adder(
        self, client: TestClient
    ) -> None:
        snapshot = _seed(client, "default")
        chat_id = _default_chat_id(snapshot)
        alice = _user(snapshot, "alice")  # creator
        newcomer = client.post("/api/users", json={"first_name": "Eve", "username": "eve"}).json()

        resp = client.post(
            f"/api/chats/{chat_id}/join",
            json={"user_id": newcomer["id"], "by_user_id": alice["id"]},
        )
        assert resp.status_code == 200

        update = _last_update()
        Update.model_validate(update)
        message = update["message"]
        assert message["from"]["id"] == alice["id"]
        assert message["new_chat_members"][0]["id"] == newcomer["id"]

    def test_joining_twice_is_rejected(self, client: TestClient) -> None:
        snapshot = _seed(client, "default")
        chat_id = _default_chat_id(snapshot)
        bob = _user(snapshot, "bob")
        resp = client.post(f"/api/chats/{chat_id}/join", json={"user_id": bob["id"]})
        assert resp.status_code == 400

    def test_self_leave_sets_left_status_and_from_is_the_leaver(self, client: TestClient) -> None:
        snapshot = _seed(client, "default")
        chat_id = _default_chat_id(snapshot)
        bob = _user(snapshot, "bob")

        resp = client.post(f"/api/chats/{chat_id}/leave", json={"user_id": bob["id"]})
        assert resp.status_code == 200
        chat = resp.json()
        membership = next(m for m in chat["members"] if m["user_id"] == bob["id"])
        assert membership["role"] == "left"

        update = _last_update()
        Update.model_validate(update)
        message = update["message"]
        assert message["from"]["id"] == bob["id"]
        assert message["left_chat_member"]["id"] == bob["id"]

    def test_kicked_by_another_sets_kicked_status_and_from_is_the_kicker(
        self, client: TestClient
    ) -> None:
        snapshot = _seed(client, "default")
        chat_id = _default_chat_id(snapshot)
        alice = _user(snapshot, "alice")
        bob = _user(snapshot, "bob")

        resp = client.post(
            f"/api/chats/{chat_id}/leave",
            json={"user_id": bob["id"], "by_user_id": alice["id"]},
        )
        assert resp.status_code == 200
        chat = resp.json()
        membership = next(m for m in chat["members"] if m["user_id"] == bob["id"])
        assert membership["role"] == "kicked"

        update = _last_update()
        Update.model_validate(update)
        message = update["message"]
        assert message["from"]["id"] == alice["id"]
        assert message["left_chat_member"]["id"] == bob["id"]

    def test_leaving_when_not_a_member_is_rejected(self, client: TestClient) -> None:
        snapshot = _seed(client, "default")
        chat_id = _default_chat_id(snapshot)
        outsider = client.post("/api/users", json={"first_name": "Eve", "username": "eve"}).json()
        resp = client.post(f"/api/chats/{chat_id}/leave", json={"user_id": outsider["id"]})
        assert resp.status_code == 400

    @pytest.mark.parametrize(
        ("role", "anonymous"),
        [("administrator", None), (None, True), ("creator", False)],
    )
    def test_patch_member_updates_role_and_anonymity(
        self, client: TestClient, role: str | None, anonymous: bool | None
    ) -> None:
        snapshot = _seed(client, "default")
        chat_id = _default_chat_id(snapshot)
        bob = _user(snapshot, "bob")

        body: dict[str, Any] = {}
        if role is not None:
            body["role"] = role
        if anonymous is not None:
            body["anonymous"] = anonymous

        resp = client.post(f"/api/chats/{chat_id}/members/{bob['id']}", json=body)
        assert resp.status_code == 200
        membership = next(m for m in resp.json()["members"] if m["user_id"] == bob["id"])
        if role is not None:
            assert membership["role"] == role
        if anonymous is not None:
            assert membership["anonymous"] == anonymous


# --------------------------------------------------------------------- callbacks


class TestCallback:
    def _message_with_buttons(self, chat_id: int, from_id: int) -> int:
        sandbox = store()
        message = SandboxMessage(
            message_id=sandbox.next_message_id(),
            chat_id=chat_id,
            from_id=from_id,
            text="Pick one",
            date=time.time(),
            reply_markup={
                "inline_keyboard": [
                    [{"text": "Yes", "callback_data": "yes"}, {"text": "No", "callback_data": "no"}]
                ]
            },
        )
        sandbox.add_message(message)
        return message.message_id

    def test_callback_press_queues_a_valid_update(self, client: TestClient) -> None:
        snapshot = _seed(client, "default")
        chat_id = _default_chat_id(snapshot)
        bob = _user(snapshot, "bob")
        message_id = self._message_with_buttons(chat_id, get_config().bot.id)

        resp = client.post(
            f"/api/chats/{chat_id}/callback",
            json={"user_id": bob["id"], "message_id": message_id, "data": "yes"},
        )
        assert resp.status_code == 200
        update = resp.json()
        Update.model_validate(update)

        callback_query = update["callback_query"]
        assert callback_query["data"] == "yes"
        assert callback_query["from"]["id"] == bob["id"]
        assert callback_query["message"]["message_id"] == message_id
        assert "chat_instance" in callback_query
        assert "id" in callback_query

    def test_callback_with_unknown_data_is_rejected(self, client: TestClient) -> None:
        snapshot = _seed(client, "default")
        chat_id = _default_chat_id(snapshot)
        bob = _user(snapshot, "bob")
        message_id = self._message_with_buttons(chat_id, get_config().bot.id)

        resp = client.post(
            f"/api/chats/{chat_id}/callback",
            json={"user_id": bob["id"], "message_id": message_id, "data": "not-a-real-button"},
        )
        assert resp.status_code == 400

    def test_callback_on_message_without_keyboard_is_rejected(self, client: TestClient) -> None:
        snapshot = _seed(client, "default")
        chat_id = _default_chat_id(snapshot)
        bob = _user(snapshot, "bob")
        sandbox = store()
        message = SandboxMessage(
            message_id=sandbox.next_message_id(),
            chat_id=chat_id,
            from_id=get_config().bot.id,
            text="no buttons here",
            date=time.time(),
        )
        sandbox.add_message(message)

        resp = client.post(
            f"/api/chats/{chat_id}/callback",
            json={"user_id": bob["id"], "message_id": message.message_id, "data": "yes"},
        )
        assert resp.status_code == 400
