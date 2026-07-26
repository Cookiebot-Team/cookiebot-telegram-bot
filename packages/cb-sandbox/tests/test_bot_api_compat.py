"""Cross-cutting Bot API compatibility tests: the confirmed update-id bug fix,
the failure envelope, `parse_mode`/entity parsing, and the new methods added
to close the gap with https://core.telegram.org/bots/api.

`test_telegram_api.py` already covers per-method shape/validation in detail
(and its docstring explains why every payload here is checked against
aiogram's real models, not this file's own idea of the shape — the same rule
applies here). This file groups the scenarios that don't belong to one method,
plus every genuinely new method, so the "what changed and why" is legible in
one place rather than scattered across edits to the older file.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from aiogram.types import Message, MessageEntity
from cb_sandbox.state import Membership, SandboxChat, SandboxStore, SandboxUser, store
from cb_sandbox.telegram_api import router
from fastapi import FastAPI
from fastapi.testclient import TestClient

TOKEN = "123456:TEST-TOKEN"


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


# --------------------------------------------------------- the confirmed bug
#
# `SandboxStore.reset()` used to restart `_update_ids` at 1. cb-gateway runs a
# real Valkey-backed dedupe middleware keyed on `update_id`
# (`cb_core.dedupe.idempotency_key`), so after a reset the first updates the
# sandbox handed out were indistinguishable from ones Valkey already had
# recorded as delivered — they were silently dropped as redeliveries and the
# bot looked dead. These tests are the regression: an id, once minted, is
# never minted again, across a reset and across a process restart.


class TestUpdateIdSurvivesReset:
    def test_reset_does_not_rewind_the_update_id_counter(self) -> None:
        s = store()
        s.reset()
        first = s.next_update_id()
        second = s.next_update_id()
        assert second == first + 1

        s.reset()
        third = s.next_update_id()
        assert third > second, "update_id must never go backwards across a reset"

    def test_get_updates_offsets_never_repeat_across_a_reset(self, client: TestClient) -> None:
        """The exact failure mode end to end: an update queued before a reset
        and one queued after must never share an `update_id` — that id space
        is what cb-gateway's dedupe middleware keys on."""
        store().reset()
        before = store().queue_update({"message": {"text": "before reset"}})
        _call(client, "getUpdates", offset=before["update_id"] + 1, timeout=0)

        store().reset()
        after = store().queue_update({"message": {"text": "after reset"}})

        assert after["update_id"] > before["update_id"]


class TestUpdateIdSurvivesRestart:
    def test_new_store_on_the_same_file_resumes_past_the_high_water_mark(
        self, tmp_path: Any
    ) -> None:
        db_path = str(tmp_path / "sandbox.duckdb")
        first = SandboxStore(db_path)
        try:
            minted = [first.next_update_id() for _ in range(3)]
        finally:
            first.close()

        second = SandboxStore(db_path)
        try:
            assert second.next_update_id() > max(minted)
        finally:
            second.close()

    def test_a_reset_then_restart_still_resumes_correctly(self, tmp_path: Any) -> None:
        """The two effects compose: a reset that doesn't rewind the in-memory
        counter, followed by a restart that has to recover the same
        high-water mark from disk with no in-memory state to fall back on."""
        db_path = str(tmp_path / "sandbox.duckdb")
        first = SandboxStore(db_path)
        try:
            first.next_update_id()
            first.next_update_id()
            first.reset()
            last_before_restart = first.next_update_id()
        finally:
            first.close()

        second = SandboxStore(db_path)
        try:
            assert second.next_update_id() > last_before_restart
        finally:
            second.close()


# ------------------------------------------------------- message id, same bug


class TestMessageIdSurvivesReset:
    def test_reset_does_not_rewind_the_message_id_counter(self) -> None:
        s = store()
        s.reset()
        first = s.next_message_id()
        s.reset()
        second = s.next_message_id()
        assert second > first


# --------------------------------------------------------------- envelope


def test_error_envelope_carries_parameters_when_given() -> None:
    """`ResponseParameters` (`retry_after`/`migrate_to_chat_id`) wiring: no
    method in this file raises one today (no simulated flood control or chat
    migration — see docs/SANDBOX.md), but the dispatch boundary must still
    shape it correctly the day something does."""
    from cb_sandbox.telegram_api import _telegram_error

    response = _telegram_error(429, "Too Many Requests: retry later", parameters={"retry_after": 5})
    body = json.loads(response.body)
    assert body == {
        "ok": False,
        "error_code": 429,
        "description": "Too Many Requests: retry later",
        "parameters": {"retry_after": 5},
    }


def test_success_envelope_has_no_parameters_key(client: TestClient) -> None:
    body = _call(client, "getMe")
    assert "parameters" not in body


# ------------------------------------------------------------------- getMe


def test_get_me_carries_the_fields_aiogram_branches_on(client: TestClient) -> None:
    body = _call(client, "getMe")
    result = body["result"]
    assert result["can_join_groups"] is True
    assert result["can_read_all_group_messages"] is True
    assert result["supports_inline_queries"] is False


# --------------------------------------------------------- parse_mode: HTML


class TestHtmlParseMode:
    def test_bold_and_italic_become_entities_not_markup(self, client: TestClient) -> None:
        _seed_chat()
        body = _call(
            client,
            "sendMessage",
            chat_id=-1001000000001,
            text="<b>bold</b> and <i>italic</i>",
            parse_mode="HTML",
        )
        result = body["result"]
        # The stored/returned text is what real Telegram would show a user:
        # tags gone, formatting moved into `entities`.
        assert result["text"] == "bold and italic"
        assert "<b>" not in result["text"]
        message = Message.model_validate(result)
        assert message.entities is not None
        types = {(e.type, e.offset, e.length) for e in message.entities}
        assert ("bold", 0, 4) in types
        assert ("italic", 9, 6) in types

    def test_nested_tags_produce_two_entities(self, client: TestClient) -> None:
        _seed_chat()
        body = _call(
            client,
            "sendMessage",
            chat_id=-1001000000001,
            text="<b>bold <i>and italic</i></b>",
            parse_mode="HTML",
        )
        result = body["result"]
        assert result["text"] == "bold and italic"
        message = Message.model_validate(result)
        types = {e.type for e in message.entities or []}
        assert types == {"bold", "italic"}

    def test_link_becomes_text_link_entity(self, client: TestClient) -> None:
        _seed_chat()
        body = _call(
            client,
            "sendMessage",
            chat_id=-1001000000001,
            text='<a href="https://example.com">click</a>',
            parse_mode="HTML",
        )
        result = body["result"]
        assert result["text"] == "click"
        entity = MessageEntity.model_validate(result["entities"][0])
        assert entity.type == "text_link"
        assert entity.url == "https://example.com"

    def test_spoiler_span_becomes_spoiler_entity(self, client: TestClient) -> None:
        _seed_chat()
        body = _call(
            client,
            "sendMessage",
            chat_id=-1001000000001,
            text="<span class='tg-spoiler'>hidden</span>",
            parse_mode="HTML",
        )
        assert body["result"]["text"] == "hidden"
        assert body["result"]["entities"][0]["type"] == "spoiler"

    def test_html_entity_escapes_are_decoded(self, client: TestClient) -> None:
        _seed_chat()
        body = _call(
            client,
            "sendMessage",
            chat_id=-1001000000001,
            text="1 &lt; 2 &amp; 3 &gt; 1",
            parse_mode="HTML",
        )
        assert body["result"]["text"] == "1 < 2 & 3 > 1"

    def test_unsupported_tag_is_a_telegram_shaped_400(self, client: TestClient) -> None:
        _seed_chat()
        response = client.post(
            f"/bot{TOKEN}/sendMessage",
            json={"chat_id": -1001000000001, "text": "<script>bad</script>", "parse_mode": "HTML"},
        )
        assert response.status_code == 400
        assert "can't parse entities" in response.json()["description"]

    def test_unclosed_tag_is_a_telegram_shaped_400(self, client: TestClient) -> None:
        _seed_chat()
        response = client.post(
            f"/bot{TOKEN}/sendMessage",
            json={"chat_id": -1001000000001, "text": "<b>never closed", "parse_mode": "HTML"},
        )
        assert response.status_code == 400
        assert "can't parse entities" in response.json()["description"]

    def test_astral_emoji_offsets_are_utf16_not_codepoints(self, client: TestClient) -> None:
        """A surrogate-pair emoji is 1 Python codepoint but 2 UTF-16 code
        units — Telegram's own offset unit. Getting this wrong is invisible
        for plain ASCII text and silently wrong the moment real chat content
        (emoji) is involved."""
        _seed_chat()
        body = _call(
            client,
            "sendMessage",
            chat_id=-1001000000001,
            text="😀<b>bold</b>",
            parse_mode="HTML",
        )
        entity = body["result"]["entities"][0]
        assert entity == {"type": "bold", "offset": 2, "length": 4}

    def test_parse_mode_and_entities_are_mutually_exclusive(self, client: TestClient) -> None:
        _seed_chat()
        response = client.post(
            f"/bot{TOKEN}/sendMessage",
            json={
                "chat_id": -1001000000001,
                "text": "hi",
                "parse_mode": "HTML",
                "entities": json.dumps([{"type": "bold", "offset": 0, "length": 2}]),
            },
        )
        assert response.status_code == 400

    def test_explicit_entities_with_no_parse_mode_are_stored_verbatim(
        self, client: TestClient
    ) -> None:
        _seed_chat()
        entities = [{"type": "bold", "offset": 0, "length": 2}]
        body = _call(
            client, "sendMessage", chat_id=-1001000000001, text="hi", entities=json.dumps(entities)
        )
        assert body["result"]["text"] == "hi"
        assert body["result"]["entities"] == entities


# ---------------------------------------------------- parse_mode: MarkdownV2


class TestMarkdownV2ParseMode:
    def test_bold_and_italic(self, client: TestClient) -> None:
        _seed_chat()
        body = _call(
            client,
            "sendMessage",
            chat_id=-1001000000001,
            text="*bold* and _italic_",
            parse_mode="MarkdownV2",
        )
        result = body["result"]
        assert result["text"] == "bold and italic"
        message = Message.model_validate(result)
        types = {(e.type, e.offset, e.length) for e in message.entities or []}
        assert ("bold", 0, 4) in types
        assert ("italic", 9, 6) in types

    def test_inline_code_and_link(self, client: TestClient) -> None:
        _seed_chat()
        body = _call(
            client,
            "sendMessage",
            chat_id=-1001000000001,
            text="`code` and [a link](https://example.com)",
            parse_mode="MarkdownV2",
        )
        result = body["result"]
        assert result["text"] == "code and a link"
        entity_types = {e["type"] for e in result["entities"]}
        assert entity_types == {"code", "text_link"}

    def test_escaped_character_is_literal(self, client: TestClient) -> None:
        _seed_chat()
        body = _call(
            client,
            "sendMessage",
            chat_id=-1001000000001,
            text="2 \\* 2 = 4",
            parse_mode="MarkdownV2",
        )
        assert body["result"]["text"] == "2 * 2 = 4"
        assert body["result"]["entities"] == []

    def test_unclosed_marker_is_a_telegram_shaped_400(self, client: TestClient) -> None:
        _seed_chat()
        response = client.post(
            f"/bot{TOKEN}/sendMessage",
            json={"chat_id": -1001000000001, "text": "*never closed", "parse_mode": "MarkdownV2"},
        )
        assert response.status_code == 400


# -------------------------------------------------------------- captions


def test_caption_parse_mode_produces_caption_entities_not_entities(client: TestClient) -> None:
    _seed_chat()
    body = _call(
        client,
        "sendPhoto",
        chat_id=-1001000000001,
        file_id="f1",
        caption="<b>bold caption</b>",
        parse_mode="HTML",
    )
    result = body["result"]
    assert result["caption"] == "bold caption"
    assert result["caption_entities"][0]["type"] == "bold"
    assert "entities" not in result  # entities is the *text* field's, unused on a media message


# ------------------------------------------------------- chat_id as @username


def test_send_message_resolves_chat_by_username(client: TestClient) -> None:
    s = store()
    chat = SandboxChat(id=-1002000000002, title="Public Group", username="qagroup")
    s.chats[chat.id] = chat
    body = _call(client, "sendMessage", chat_id="@qagroup", text="hi")
    assert body["ok"] is True
    assert body["result"]["chat"]["id"] == chat.id


def test_send_message_unknown_username_is_400(client: TestClient) -> None:
    response = client.post(f"/bot{TOKEN}/sendMessage", json={"chat_id": "@nobody", "text": "hi"})
    assert response.status_code == 400


# ------------------------------------------------------------- private chat


def test_private_chat_uses_first_name_not_title() -> None:
    """Real Telegram never sends `title` on a private chat — only a group,
    supergroup or channel has one; a DM peer is a `first_name`/`username`,
    same as any other `User`."""
    chat = SandboxChat(id=555, title="Dana", type="private", username="dana")
    payload = chat.as_telegram()
    assert payload["first_name"] == "Dana"
    assert "title" not in payload
    assert payload["username"] == "dana"


# --------------------------------------------------------- reply_parameters


class TestReplyParameters:
    def test_reply_parameters_message_id_sets_the_reply(self, client: TestClient) -> None:
        _seed_chat()
        original = _call(client, "sendMessage", chat_id=-1001000000001, text="original")
        body = _call(
            client,
            "sendMessage",
            chat_id=-1001000000001,
            text="a reply",
            reply_parameters=json.dumps({"message_id": original["result"]["message_id"]}),
        )
        assert body["result"]["reply_to_message"]["message_id"] == original["result"]["message_id"]

    def test_reply_to_missing_message_is_a_telegram_shaped_400(self, client: TestClient) -> None:
        _seed_chat()
        response = client.post(
            f"/bot{TOKEN}/sendMessage",
            json={
                "chat_id": -1001000000001,
                "text": "a reply",
                "reply_parameters": json.dumps({"message_id": 999999}),
            },
        )
        assert response.status_code == 400

    def test_allow_sending_without_reply_suppresses_the_error(self, client: TestClient) -> None:
        _seed_chat()
        body = _call(
            client,
            "sendMessage",
            chat_id=-1001000000001,
            text="a reply",
            reply_parameters=json.dumps(
                {"message_id": 999999, "allow_sending_without_reply": True}
            ),
        )
        assert body["ok"] is True
        assert "reply_to_message" not in body["result"]


# --------------------------------------------------- link_preview_options


def test_link_preview_options_round_trips(client: TestClient) -> None:
    _seed_chat()
    options = {"is_disabled": True}
    body = _call(
        client,
        "sendMessage",
        chat_id=-1001000000001,
        text="https://example.com",
        link_preview_options=json.dumps(options),
    )
    assert body["result"]["link_preview_options"] == options
    Message.model_validate(body["result"])


# ------------------------------------------------------- message_thread_id


def test_message_thread_id_sets_is_topic_message(client: TestClient) -> None:
    _seed_chat()
    body = _call(
        client, "sendMessage", chat_id=-1001000000001, text="in a topic", message_thread_id=42
    )
    result = body["result"]
    assert result["message_thread_id"] == 42
    assert result["is_topic_message"] is True
    Message.model_validate(result)


# ---------------------------------------------------------------- getUpdates


class TestGetUpdatesCompat:
    def test_concurrent_polls_get_409_conflict(self, client: TestClient) -> None:
        s = store()
        assert s.begin_polling() is True
        try:
            response = client.post(f"/bot{TOKEN}/getUpdates", json={"timeout": 0})
            assert response.status_code == 409
            assert response.json()["description"] == (
                "Conflict: terminated by other getUpdates request; "
                "make sure that only one bot instance is running"
            )
        finally:
            s.end_polling()

    def test_limit_out_of_range_is_400(self, client: TestClient) -> None:
        response = client.post(f"/bot{TOKEN}/getUpdates", json={"limit": 0, "timeout": 0})
        assert response.status_code == 400
        response = client.post(f"/bot{TOKEN}/getUpdates", json={"limit": 101, "timeout": 0})
        assert response.status_code == 400

    def test_negative_offset_keeps_only_the_tail_of_the_queue(self, client: TestClient) -> None:
        s = store()
        s.reset()
        s.queue_update({"message": {"text": "one"}})
        s.queue_update({"message": {"text": "two"}})
        third = s.queue_update({"message": {"text": "three"}})
        body = _call(client, "getUpdates", offset=-1, timeout=0)
        assert [u["update_id"] for u in body["result"]] == [third["update_id"]]

    def test_allowed_updates_filters_the_response(self, client: TestClient) -> None:
        s = store()
        s.reset()
        s.queue_update({"message": {"text": "a message"}})
        s.queue_update({"callback_query": {"id": "cb-filter"}})
        body = _call(
            client, "getUpdates", timeout=0, allowed_updates=json.dumps(["callback_query"])
        )
        assert len(body["result"]) == 1
        assert "callback_query" in body["result"][0]

    def test_allowed_updates_is_remembered_across_calls(self, client: TestClient) -> None:
        """Bot API docs: "If not specified, the previous setting will be
        used" — a filter set once must still apply on a later call that omits
        the parameter entirely."""
        s = store()
        s.reset()
        _call(client, "getUpdates", timeout=0, allowed_updates=json.dumps(["callback_query"]))
        s.queue_update({"message": {"text": "should stay filtered out"}})
        body = _call(client, "getUpdates", timeout=0)
        assert body["result"] == []


# --------------------------------------------------------- new send* methods


def test_send_document(client: TestClient) -> None:
    _seed_chat()
    body = _call(client, "sendDocument", chat_id=-1001000000001, document="f1")
    assert body["ok"] is True
    Message.model_validate(body["result"])


def test_send_audio(client: TestClient) -> None:
    _seed_chat()
    body = _call(client, "sendAudio", chat_id=-1001000000001, audio="f1")
    assert body["ok"] is True
    Message.model_validate(body["result"])


def test_send_voice(client: TestClient) -> None:
    _seed_chat()
    body = _call(client, "sendVoice", chat_id=-1001000000001, voice="f1")
    assert body["ok"] is True
    Message.model_validate(body["result"])


def test_send_dice_default_emoji_and_range(client: TestClient) -> None:
    _seed_chat()
    body = _call(client, "sendDice", chat_id=-1001000000001)
    dice = body["result"]["dice"]
    assert dice["emoji"] == "🎲"
    assert 1 <= dice["value"] <= 6
    Message.model_validate(body["result"])


def test_send_dice_basketball_range(client: TestClient) -> None:
    _seed_chat()
    body = _call(client, "sendDice", chat_id=-1001000000001, emoji="🏀")
    assert 1 <= body["result"]["dice"]["value"] <= 5


# ------------------------------------------------------------- deleteMessages


def test_delete_messages_deletes_existing_and_skips_missing(client: TestClient) -> None:
    _seed_chat()
    sent = _call(client, "sendMessage", chat_id=-1001000000001, text="one")
    message_id = sent["result"]["message_id"]
    body = _call(
        client,
        "deleteMessages",
        chat_id=-1001000000001,
        message_ids=json.dumps([message_id, 999999]),
    )
    assert body == {"ok": True, "result": True}
    assert store().message(-1001000000001, message_id).deleted is True  # type: ignore[union-attr]


# --------------------------------------------------------- forward / copy


class TestForwardAndCopy:
    def test_forward_message_carries_origin_and_keeps_bot_as_sender(
        self, client: TestClient
    ) -> None:
        source = _seed_chat(chat_id=-1001000000001, title="Source")
        target = _seed_chat(chat_id=-1001000000002, title="Target")
        original = _call(client, "sendMessage", chat_id=source.id, text="forward me")
        body = _call(
            client,
            "forwardMessage",
            chat_id=target.id,
            from_chat_id=source.id,
            message_id=original["result"]["message_id"],
        )
        result = body["result"]
        assert result["text"] == "forward me"
        assert result["chat"]["id"] == target.id
        assert result["from"]["is_bot"] is True
        assert result["forward_origin"]["type"] == "user"
        Message.model_validate(result)

    def test_forward_missing_message_is_400(self, client: TestClient) -> None:
        source = _seed_chat(chat_id=-1001000000001, title="Source")
        target = _seed_chat(chat_id=-1001000000002, title="Target")
        response = client.post(
            f"/bot{TOKEN}/forwardMessage",
            json={"chat_id": target.id, "from_chat_id": source.id, "message_id": 999999},
        )
        assert response.status_code == 400

    def test_copy_message_returns_only_a_message_id(self, client: TestClient) -> None:
        source = _seed_chat(chat_id=-1001000000001, title="Source")
        target = _seed_chat(chat_id=-1001000000002, title="Target")
        original = _call(client, "sendMessage", chat_id=source.id, text="copy me")
        body = _call(
            client,
            "copyMessage",
            chat_id=target.id,
            from_chat_id=source.id,
            message_id=original["result"]["message_id"],
        )
        assert set(body["result"]) == {"message_id"}
        copied = store().message(target.id, body["result"]["message_id"])
        assert copied is not None
        assert copied.text == "copy me"
        # A copy carries no forward attribution at all, unlike forwardMessage.
        assert copied.forward_origin is None


# ------------------------------------------------------------ editMessageCaption


def test_edit_message_caption(client: TestClient) -> None:
    _seed_chat()
    sent = _call(client, "sendPhoto", chat_id=-1001000000001, file_id="f1", caption="v1")
    body = _call(
        client,
        "editMessageCaption",
        chat_id=-1001000000001,
        message_id=sent["result"]["message_id"],
        caption="<b>v2</b>",
        parse_mode="HTML",
    )
    assert body["result"]["caption"] == "v2"
    assert body["result"]["caption_entities"][0]["type"] == "bold"


# ------------------------------------------------------------------- pin/unpin


def test_pin_then_unpin_chat_message(client: TestClient) -> None:
    chat = _seed_chat()
    sent = _call(client, "sendMessage", chat_id=chat.id, text="pin me")
    message_id = sent["result"]["message_id"]

    pin_body = _call(client, "pinChatMessage", chat_id=chat.id, message_id=message_id)
    assert pin_body == {"ok": True, "result": True}
    assert store().chats[chat.id].pinned_message_id == message_id

    unpin_body = _call(client, "unpinChatMessage", chat_id=chat.id)
    assert unpin_body == {"ok": True, "result": True}
    assert store().chats[chat.id].pinned_message_id is None


def test_pin_missing_message_is_400(client: TestClient) -> None:
    chat = _seed_chat()
    response = client.post(
        f"/bot{TOKEN}/pinChatMessage", json={"chat_id": chat.id, "message_id": 999999}
    )
    assert response.status_code == 400


# --------------------------------------------------------- chat administration


def test_set_chat_title(client: TestClient) -> None:
    chat = _seed_chat()
    body = _call(client, "setChatTitle", chat_id=chat.id, title="New Title")
    assert body == {"ok": True, "result": True}
    assert store().chats[chat.id].title == "New Title"


def test_set_chat_description(client: TestClient) -> None:
    chat = _seed_chat()
    body = _call(client, "setChatDescription", chat_id=chat.id, description="About this group")
    assert body == {"ok": True, "result": True}
    assert store().chats[chat.id].description == "About this group"


def test_export_chat_invite_link(client: TestClient) -> None:
    chat = _seed_chat()
    body = _call(client, "exportChatInviteLink", chat_id=chat.id)
    assert body["ok"] is True
    assert isinstance(body["result"], str)
    assert body["result"].startswith("https://")


def test_leave_chat_sets_the_bots_own_membership_to_left(client: TestClient) -> None:
    chat = _seed_chat()
    from cb_sandbox.telegram_api import bot_id

    self_id = bot_id()
    chat.members[self_id] = Membership(user_id=self_id, role="administrator")
    body = _call(client, "leaveChat", chat_id=chat.id)
    assert body == {"ok": True, "result": True}
    assert store().membership(chat.id, self_id).role == "left"  # type: ignore[union-attr]


def test_get_chat_member_count_excludes_left_and_kicked(client: TestClient) -> None:
    chat = _seed_chat()
    chat.members[1] = Membership(user_id=1, role="member")
    chat.members[2] = Membership(user_id=2, role="left")
    chat.members[3] = Membership(user_id=3, role="kicked")
    body = _call(client, "getChatMemberCount", chat_id=chat.id)
    assert body["result"] == 1


def test_set_chat_permissions(client: TestClient) -> None:
    chat = _seed_chat()
    body = _call(
        client,
        "setChatPermissions",
        chat_id=chat.id,
        permissions=json.dumps({"can_send_messages": True}),
    )
    assert body == {"ok": True, "result": True}
    assert store().chats[chat.id].default_permissions["can_send_messages"] is True


def test_set_message_reaction_is_recorded_and_does_not_500(client: TestClient) -> None:
    chat = _seed_chat()
    sent = _call(client, "sendMessage", chat_id=chat.id, text="react to me")
    body = _call(
        client,
        "setMessageReaction",
        chat_id=chat.id,
        message_id=sent["result"]["message_id"],
        reaction=json.dumps([{"type": "emoji", "emoji": "👍"}]),
    )
    assert body == {"ok": True, "result": True}
    calls = [c["method"] for c in store().api_calls]
    assert "setMessageReaction" in calls


# ------------------------------------------------------------- unban semantics


class TestUnbanOnlyIfBanned:
    def test_only_if_banned_true_leaves_a_non_banned_member_untouched(
        self, client: TestClient
    ) -> None:
        chat = _seed_chat()
        chat.members[7] = Membership(user_id=7, role="administrator")
        _call(client, "unbanChatMember", chat_id=chat.id, user_id=7, only_if_banned=True)
        assert store().membership(chat.id, 7).role == "administrator"  # type: ignore[union-attr]

    def test_only_if_banned_true_lifts_an_actual_ban(self, client: TestClient) -> None:
        chat = _seed_chat()
        chat.members[7] = Membership(user_id=7, role="kicked")
        _call(client, "unbanChatMember", chat_id=chat.id, user_id=7, only_if_banned=True)
        assert store().membership(chat.id, 7).role == "left"  # type: ignore[union-attr]

    def test_default_resets_to_left_unconditionally(self, client: TestClient) -> None:
        chat = _seed_chat()
        chat.members[7] = Membership(user_id=7, role="administrator")
        _call(client, "unbanChatMember", chat_id=chat.id, user_id=7)
        assert store().membership(chat.id, 7).role == "left"  # type: ignore[union-attr]


# ------------------------------------------------- private chats the bot may use


class TestPrivateChatAccess:
    """Telegram lets a bot answer a conversation but never start one. The
    sandbox has to reproduce the refusal rather than a generic "chat not
    found", because handlers that DM someone (`/config`'s "message me
    privately" fallback) branch on it, and a tester who sees the wrong error
    reads a real constraint as a broken workbench.
    """

    def test_sending_to_a_user_with_no_dm_is_forbidden(self, client: TestClient) -> None:
        s = store()
        s.users[500000001] = SandboxUser(id=500000001, first_name="Bob", username="bob")

        response = client.post(f"/bot{TOKEN}/sendMessage", json={"chat_id": 500000001, "text": "x"})
        assert response.status_code == 403
        assert response.json() == {
            "ok": False,
            "error_code": 403,
            "description": "Forbidden: bot can't initiate conversation with a user",
        }

    def test_sending_to_an_opened_dm_succeeds(self, client: TestClient) -> None:
        s = store()
        s.users[500000001] = SandboxUser(id=500000001, first_name="Bob", username="bob")
        # What `POST /api/users/{id}/dm` creates: a private chat whose id *is*
        # the user id, which is the only id the bot ever has to reach them.
        s.chats[500000001] = SandboxChat(id=500000001, title="Bob", type="private")

        payload = _call(client, "sendMessage", chat_id=500000001, text="hello")
        assert payload["ok"] is True
        Message.model_validate(payload["result"])
        assert payload["result"]["chat"]["type"] == "private"

    def test_an_unknown_id_is_still_chat_not_found(self, client: TestClient) -> None:
        response = client.post(f"/bot{TOKEN}/sendMessage", json={"chat_id": 999, "text": "x"})
        assert response.status_code == 400
        assert response.json()["description"] == "Bad Request: chat not found"
