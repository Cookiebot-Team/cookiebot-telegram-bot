"""Unit coverage for core_rules — pure logic only, no dispatcher, no Telegram, no DB.

See docs/contracts/core_rules.md for the full behaviour contract and
qa/features/core_rules.feature + qa/test_core_rules.py for the end-to-end
version of the same assertions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock

import pytest

from cb_gateway.filters import CommandName
from cb_gateway.handlers import rules as rules_handler


@dataclass
class _FakeUser:
    id: int
    username: str | None = None
    first_name: str | None = None


@dataclass
class _FakeMessage:
    """Only the attributes the handler / filters actually read."""

    text: str | None
    reply_to_message: Any = None
    from_user: _FakeUser | None = None
    chat: Any = field(default_factory=lambda: type("Chat", (), {"id": -100})())
    bot: Any = None
    message_id: int = 0

    async def reply(self, text: str) -> None:  # pragma: no cover - overridden per test
        raise NotImplementedError


# ------------------------------------------------------------- trigger surface


@pytest.mark.parametrize("text", ["/rules", "/regras", "/reglas"])
@pytest.mark.asyncio
async def test_every_v1_rules_alias_resolves(text: str) -> None:
    result = await CommandName("rules")(_FakeMessage(text), bot_username="CookieMWbot")
    assert result is not False, f"{text!r} did not resolve to the rules command"


@pytest.mark.parametrize("text", ["/newrules", "/novasregras", "/nuevasreglas"])
@pytest.mark.asyncio
async def test_every_v1_newrules_alias_resolves(text: str) -> None:
    result = await CommandName("newrules")(_FakeMessage(text), bot_username="CookieMWbot")
    assert result is not False, f"{text!r} did not resolve to the newrules command"


@pytest.mark.asyncio
async def test_rules_addressed_at_a_different_bot_does_not_resolve() -> None:
    result = await CommandName("rules")(
        _FakeMessage("/rules@SomeOtherBot"), bot_username="CookieMWbot"
    )
    assert result is False


@pytest.mark.asyncio
async def test_unrelated_command_does_not_resolve_as_rules() -> None:
    result = await CommandName("rules")(_FakeMessage("/isalive"), bot_username="CookieMWbot")
    assert result is False


# ------------------------------------------------------------- reply capture


class TestIsNewRulesReply:
    """`_is_new_rules_reply` — the structural precondition ported from
    `COOKIEBOT.py:186,293` (see rules.py's module docstring)."""

    def test_no_reply_does_not_match(self) -> None:
        assert not rules_handler._is_new_rules_reply(_FakeMessage("some text"))  # noqa: SLF001

    def test_reply_to_unrelated_message_does_not_match(self) -> None:
        reply = _FakeMessage("something else")
        message = _FakeMessage("new rules text", reply_to_message=reply)
        assert not rules_handler._is_new_rules_reply(message)  # noqa: SLF001

    def test_reply_to_the_exact_prompt_matches(self) -> None:
        reply = _FakeMessage(rules_handler.NEW_RULES_PROMPT)
        message = _FakeMessage("Be nice to each other.", reply_to_message=reply)
        assert rules_handler._is_new_rules_reply(message)  # noqa: SLF001

    def test_text_with_no_body_does_not_match(self) -> None:
        reply = _FakeMessage(rules_handler.NEW_RULES_PROMPT)
        assert not rules_handler._is_new_rules_reply(_FakeMessage(None, reply_to_message=reply))  # noqa: SLF001

    def test_reply_whose_own_text_looks_like_a_command_does_not_match(self) -> None:
        """v1's reply-capture `elif` is only reached when the incoming text does
        not itself start with `/` (COOKIEBOT.py:186) — a coincidental reply to
        the prompt from a message that is itself a command is not captured."""
        reply = _FakeMessage(rules_handler.NEW_RULES_PROMPT)
        message = _FakeMessage("/rules", reply_to_message=reply)
        assert not rules_handler._is_new_rules_reply(message)  # noqa: SLF001

    def test_bare_slash_still_matches(self) -> None:
        """v1's guard is `startswith('/') and len(text) > 1` — a lone "/" falls
        through to the reply-capture branch, same as any other non-command
        text."""
        reply = _FakeMessage(rules_handler.NEW_RULES_PROMPT)
        assert rules_handler._is_new_rules_reply(_FakeMessage("/", reply_to_message=reply))  # noqa: SLF001


# ------------------------------------------------------------- pure transform


class TestSubstituteUserTags:
    def test_replaces_every_v1_tag_spelling_with_username(self) -> None:
        user = _FakeUser(id=1, username="tester")
        message = _FakeMessage("hi", from_user=user)
        for tag in (
            "{user}",
            "{username}",
            "{mention}",
            "$user",
            "$(user)",
            "$(username)",
            "<user>",
            "<username>",
            "<name>",
        ):
            assert rules_handler._substitute_user_tags(  # noqa: SLF001
                f"welcome {tag}!", message
            ) == ("welcome @tester!")

    def test_dollar_username_collides_with_dollar_user_like_v1(self) -> None:
        """v1's own tag list has `$user` before `$username`
        (`GroupShield.py:40`) with no closing delimiter on either, so `$user`
        matches as a prefix of `$username` first and only the prefix is
        replaced — `_USER_TAGS` iterates in the same order, so this quirk is
        preserved rather than fixed (AGENTS.md: user-visible quirks are
        usually kept, not silently corrected)."""
        user = _FakeUser(id=1, username="tester")
        message = _FakeMessage("hi", from_user=user)
        assert (
            rules_handler._substitute_user_tags("welcome $username!", message)  # noqa: SLF001
            == "welcome @testername!"
        )

    def test_falls_back_to_first_name_with_no_username(self) -> None:
        user = _FakeUser(id=1, username=None, first_name="Alex")
        message = _FakeMessage("hi", from_user=user)
        assert rules_handler._substitute_user_tags("hi {user}", message) == "hi Alex"  # noqa: SLF001

    def test_no_sender_leaves_text_untouched(self) -> None:
        message = _FakeMessage("hi", from_user=None)
        assert rules_handler._substitute_user_tags("hi {user}", message) == "hi {user}"  # noqa: SLF001

    def test_text_without_tags_is_unchanged(self) -> None:
        user = _FakeUser(id=1, username="tester")
        message = _FakeMessage("hi", from_user=user)
        assert rules_handler._substitute_user_tags("no tags here", message) == "no tags here"  # noqa: SLF001


# ------------------------------------------------------------------- handlers


@pytest.mark.asyncio
async def test_rules_replies_with_no_rules_when_none_stored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_fetch(group_id: int) -> str | None:
        return None

    monkeypatch.setattr(rules_handler, "_fetch_rules", fake_fetch)

    class _Ctx:
        group_id = -100
        is_admin = False
        lang = "en"
        actor = type("A", (), {"user_id": None})()

    monkeypatch.setattr(rules_handler, "context_for", AsyncMock(return_value=_Ctx()))

    message = _FakeMessage("/rules", from_user=_FakeUser(id=1, username="tester"))
    message.reply = AsyncMock()

    await rules_handler.rules(message)

    message.reply.assert_awaited_once()
    (sent_text,), _ = message.reply.await_args
    from cb_core import locales

    assert sent_text == locales.get("no_rules", "en")


@pytest.mark.asyncio
async def test_rules_appends_questions_tagline_unless_text_ends_with_mekhyw(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_fetch(group_id: int) -> str | None:
        return "Be nice.\\nNo spam."

    monkeypatch.setattr(rules_handler, "_fetch_rules", fake_fetch)

    class _Ctx:
        group_id = -100
        is_admin = False
        lang = "en"
        actor = type("A", (), {"user_id": None})()

    monkeypatch.setattr(rules_handler, "context_for", AsyncMock(return_value=_Ctx()))

    message = _FakeMessage("/rules", from_user=_FakeUser(id=1, username="tester"))
    message.reply = AsyncMock()

    await rules_handler.rules(message)

    (sent_text,), _ = message.reply.await_args
    assert "Be nice.\nNo spam." in sent_text
    assert "Questions about the bot?" in sent_text


@pytest.mark.asyncio
async def test_capture_new_rules_rejects_non_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Ctx:
        group_id = -100
        is_admin = False
        actor = type("A", (), {"user_id": 555})()

    monkeypatch.setattr(rules_handler, "context_for", AsyncMock(return_value=_Ctx()))
    upsert = AsyncMock()
    monkeypatch.setattr(rules_handler, "_upsert_rules", upsert)

    prompt = _FakeMessage(rules_handler.NEW_RULES_PROMPT)
    message = _FakeMessage("Be nice.", reply_to_message=prompt)
    message.reply = AsyncMock()

    await rules_handler.capture_new_rules(message)

    message.reply.assert_awaited_once_with(rules_handler.NOT_ADMIN_TEXT)
    upsert.assert_not_awaited()


@pytest.mark.asyncio
async def test_capture_new_rules_saves_and_confirms_for_an_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Ctx:
        group_id = -100
        is_admin = True
        actor = type("A", (), {"user_id": 555})()

    monkeypatch.setattr(rules_handler, "context_for", AsyncMock(return_value=_Ctx()))
    upsert = AsyncMock()
    monkeypatch.setattr(rules_handler, "_upsert_rules", upsert)

    prompt = _FakeMessage(rules_handler.NEW_RULES_PROMPT)
    bot = type("Bot", (), {"delete_message": AsyncMock()})()
    message = _FakeMessage("Be nice.", reply_to_message=prompt, bot=bot)
    message.reply = AsyncMock()

    await rules_handler.capture_new_rules(message)

    upsert.assert_awaited_once_with(-100, 555, "Be nice.")
    message.reply.assert_awaited_once_with(rules_handler.RULES_UPDATED_TEXT)
    bot.delete_message.assert_awaited_once()
