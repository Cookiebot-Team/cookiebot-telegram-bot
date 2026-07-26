"""Unit coverage for core_welcome — pure logic only, no dispatcher, no Telegram,
no DB.

See docs/contracts/core_welcome.md for the full behaviour contract and
qa/features/core_welcome.feature + qa/test_core_welcome.py for the end-to-end
version of the same assertions. `qa/integration/test_group_welcomes.py` covers
the real `group_welcomes` round trip.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock

import pytest

from cb_gateway.filters import CommandName
from cb_gateway.handlers import welcome as welcome_handler


@dataclass
class _FakeUser:
    id: int
    username: str | None = None
    first_name: str | None = None
    is_bot: bool = False


@dataclass
class _FakeMessage:
    """Only the attributes the handler / filters actually read."""

    text: str | None
    reply_to_message: Any = None
    from_user: _FakeUser | None = None
    new_chat_members: Any = None
    chat: Any = field(default_factory=lambda: type("Chat", (), {"id": -100, "title": "QA Group"})())
    bot: Any = None
    message_id: int = 0

    async def reply(self, text: str) -> None:  # pragma: no cover - overridden per test
        raise NotImplementedError

    async def react(self, **kwargs: Any) -> None:  # pragma: no cover - overridden per test
        raise NotImplementedError


class _FakeCtx:
    def __init__(self, *, group_id: int = -100, is_admin: bool = False, lang: str = "en") -> None:
        self.group_id = group_id
        self.is_admin = is_admin
        self.lang = lang
        self.actor = type("A", (), {"user_id": 555})()


# ------------------------------------------------------------- trigger surface


@pytest.mark.parametrize("text", ["/newwelcome", "/novobemvindo", "/nuevabienvenida"])
@pytest.mark.asyncio
async def test_every_v1_alias_resolves(text: str) -> None:
    """v1 (COOKIEBOT.py:264) accepts all three of these; all three are
    registered in `cb_core/textmatch.py:COMMAND_ALIASES`."""
    result = await CommandName("newwelcome")(_FakeMessage(text), bot_username="CookieMWbot")
    assert result is not False, f"{text!r} did not resolve to the newwelcome command"


@pytest.mark.asyncio
async def test_newwelcome_addressed_at_a_different_bot_does_not_resolve() -> None:
    result = await CommandName("newwelcome")(
        _FakeMessage("/newwelcome@SomeOtherBot"), bot_username="CookieMWbot"
    )
    assert result is False


@pytest.mark.asyncio
async def test_unrelated_command_does_not_resolve_as_newwelcome() -> None:
    result = await CommandName("newwelcome")(_FakeMessage("/isalive"), bot_username="CookieMWbot")
    assert result is False


# ------------------------------------------------------------- reply capture


class TestIsWelcomeReply:
    """`_is_welcome_reply` — the structural precondition ported from
    `COOKIEBOT.py:186,290` (see welcome.py's module docstring)."""

    def test_no_reply_does_not_match(self) -> None:
        assert not welcome_handler._is_welcome_reply(_FakeMessage("some text"))  # noqa: SLF001

    def test_reply_to_unrelated_message_does_not_match(self) -> None:
        reply = _FakeMessage("something else")
        message = _FakeMessage("new welcome text", reply_to_message=reply)
        assert not welcome_handler._is_welcome_reply(message)  # noqa: SLF001

    def test_reply_to_the_exact_prompt_matches(self) -> None:
        reply = _FakeMessage(welcome_handler.WELCOME_PROMPT)
        message = _FakeMessage("Welcome, new folks!", reply_to_message=reply)
        assert welcome_handler._is_welcome_reply(message)  # noqa: SLF001

    def test_text_with_no_body_does_not_match(self) -> None:
        reply = _FakeMessage(welcome_handler.WELCOME_PROMPT)
        assert not welcome_handler._is_welcome_reply(  # noqa: SLF001
            _FakeMessage(None, reply_to_message=reply)
        )

    def test_reply_whose_own_text_looks_like_a_command_does_not_match(self) -> None:
        """v1's reply-capture `elif` is only reached when the incoming text does
        not itself start with `/` (COOKIEBOT.py:186) — a coincidental reply to
        the prompt from a message that is itself a command is not captured."""
        reply = _FakeMessage(welcome_handler.WELCOME_PROMPT)
        message = _FakeMessage("/newwelcome", reply_to_message=reply)
        assert not welcome_handler._is_welcome_reply(message)  # noqa: SLF001

    def test_bare_slash_still_matches(self) -> None:
        """v1's guard is `startswith('/') and len(text) > 1` — a lone "/" falls
        through to the reply-capture branch, same as any other non-command
        text."""
        reply = _FakeMessage(welcome_handler.WELCOME_PROMPT)
        assert welcome_handler._is_welcome_reply(  # noqa: SLF001
            _FakeMessage("/", reply_to_message=reply)
        )


# ------------------------------------------------------------- pure transform


class TestSubstituteUserTags:
    """GroupShield.py:38-47 — the full placeholder contract. Unlike `rules.py`'s
    version, welcome always substitutes the *new joiner*'s name, never the
    requester's, so this takes a `User` directly rather than a `Message`."""

    # `$username` is deliberately excluded from these two "clean" parametrized
    # cases — see TestDollarUserCollisionDefect below, which documents why it
    # cannot resolve cleanly in v1 (or here, byte-for-byte).
    @pytest.mark.parametrize(
        "tag",
        [
            "{user}",
            "{username}",
            "{mention}",
            "$user",
            "$(user)",
            "$(username)",
            "<user>",
            "<username>",
            "<name>",
        ],
    )
    def test_every_known_tag_resolves_to_username_when_present(self, tag: str) -> None:
        user = _FakeUser(id=1, username="joe")
        assert welcome_handler._substitute_user_tags(f"hi {tag}!", user) == "hi @joe!"  # noqa: SLF001

    @pytest.mark.parametrize(
        "tag",
        [
            "{user}",
            "{username}",
            "{mention}",
            "$user",
            "$(user)",
            "$(username)",
            "<user>",
            "<username>",
            "<name>",
        ],
    )
    def test_every_known_tag_falls_back_to_first_name_without_username(self, tag: str) -> None:
        user = _FakeUser(id=1, username=None, first_name="Jo")
        assert welcome_handler._substitute_user_tags(f"hi {tag}!", user) == "hi Jo!"  # noqa: SLF001


class TestDollarUserCollisionDefect:
    """Verified v1 defect, preserved byte-for-byte (docs/contracts/core_welcome.md,
    "A second verified placeholder defect"): `$user` is checked before
    `$username` (same order in `GroupShield.py:40`) and is a literal substring
    of it with no closing delimiter to disambiguate them — unlike
    `{user}`/`{username}` or `$(user)`/`$(username)`, whose `}`/`)` breaks the
    collision. So `$username` never resolves cleanly in v1; this asserts the
    actual (corrupted) output, not an idealised one.
    """

    def test_dollar_username_is_corrupted_by_the_dollar_user_prefix_match(self) -> None:
        user = _FakeUser(id=1, username="joe")
        # "$user" matches inside "$username" first; only that prefix is
        # replaced, and the "name" suffix of the tag survives verbatim.
        assert welcome_handler._substitute_user_tags("hi $username!", user) == "hi @joename!"  # noqa: SLF001

    def test_dollar_username_collision_also_happens_with_the_first_name_fallback(self) -> None:
        user = _FakeUser(id=1, username=None, first_name="Jo")
        assert welcome_handler._substitute_user_tags("hi $username!", user) == "hi Joname!"  # noqa: SLF001

    def test_all_ten_tags_are_covered(self) -> None:
        # Pinning the exact set guards against silently dropping/adding a tag.
        assert set(welcome_handler._USER_TAGS) == {  # noqa: SLF001
            "{user}",
            "{username}",
            "{mention}",
            "$user",
            "$username",
            "$(user)",
            "$(username)",
            "<user>",
            "<username>",
            "<name>",
        }

    def test_unknown_placeholder_left_untouched(self) -> None:
        user = _FakeUser(id=1, username="joe")
        assert (
            welcome_handler._substitute_user_tags("hi {chat} and {nonsense}", user)  # noqa: SLF001
            == "hi {chat} and {nonsense}"
        )

    def test_every_occurrence_is_replaced_not_just_the_first(self) -> None:
        user = _FakeUser(id=1, username="joe")
        assert welcome_handler._substitute_user_tags("<user> meet <user>", user) == (  # noqa: SLF001
            "@joe meet @joe"
        )

    def test_multiple_distinct_tags_in_one_text_all_resolve(self) -> None:
        user = _FakeUser(id=1, username="joe")
        text = welcome_handler._substitute_user_tags("{user} aka <name> aka $user", user)  # noqa: SLF001
        assert text == "@joe aka @joe aka @joe"

    def test_substitution_is_not_word_bounded(self) -> None:
        user = _FakeUser(id=1, username="joe")
        assert welcome_handler._substitute_user_tags("<user>fan", user) == "@joefan"  # noqa: SLF001


class TestRenderCustomWelcome:
    """GroupShield.py:159-161 — the literal `\\n` unescape runs before substitution."""

    def test_literal_backslash_n_becomes_a_real_newline(self) -> None:
        user = _FakeUser(id=1, username="joe")
        assert (
            welcome_handler._render_custom_welcome("line1\\nline2", user)  # noqa: SLF001
            == "line1\nline2"
        )

    def test_unescape_runs_even_with_no_placeholder(self) -> None:
        user = _FakeUser(id=1, username="joe")
        assert (
            welcome_handler._render_custom_welcome(  # noqa: SLF001
                "no tags here\\nsecond line", user
            )
            == "no tags here\nsecond line"
        )

    def test_unescape_then_substitute_together(self) -> None:
        user = _FakeUser(id=1, first_name="Ana", username=None)
        assert (
            welcome_handler._render_custom_welcome(  # noqa: SLF001
                "Welcome <user>!\\nEnjoy.", user
            )
            == "Welcome Ana!\nEnjoy."
        )


class TestDefaultWelcome:
    """GroupShield.py:154-158 — falls back to en, ported verbatim in cb_core.locales."""

    def test_uses_welcome_user_when_chat_title_known(self) -> None:
        assert (
            welcome_handler._default_welcome("en", "QA Group")  # noqa: SLF001
            == "Hello! Welcome to the group QA Group!"
        )

    def test_uses_generic_welcome_when_no_chat_title(self) -> None:
        assert welcome_handler._default_welcome("en", None) == "Hello! Welcome to the group!"  # noqa: SLF001

    def test_uses_generic_welcome_when_chat_title_is_empty(self) -> None:
        assert welcome_handler._default_welcome("en", "") == "Hello! Welcome to the group!"  # noqa: SLF001

    def test_localises_to_group_language(self) -> None:
        assert (
            welcome_handler._default_welcome("pt", "Grupo QA")  # noqa: SLF001
            == "Olá! As boas-vindas ao grupo Grupo QA!"
        )


class TestSanitizeForPlainRetry:
    """universal_funcs.py:210,220 — v1's crude recovery from a Telegram HTML
    entity-parse error: strip every backslash and every `>`."""

    def test_strips_backslashes_and_angle_close(self) -> None:
        assert (
            welcome_handler._sanitize_for_plain_retry("weird > text \\ here")  # noqa: SLF001
            == "weird  text  here"
        )

    def test_leaves_open_angle_bracket_alone(self) -> None:
        # v1 only strips '>' — a stray unmatched '<' survives the retry too, it
        # just no longer breaks Telegram parsing because parse_mode is None.
        assert welcome_handler._sanitize_for_plain_retry("a < b") == "a < b"  # noqa: SLF001

    def test_noop_on_clean_text(self) -> None:
        assert (
            welcome_handler._sanitize_for_plain_retry("perfectly fine text")  # noqa: SLF001
            == "perfectly fine text"
        )


# ------------------------------------------------------------------- handlers


@pytest.mark.asyncio
async def test_capture_new_welcome_rejects_non_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        welcome_handler, "context_for", AsyncMock(return_value=_FakeCtx(is_admin=False))
    )
    save = AsyncMock()
    monkeypatch.setattr(welcome_handler, "_save_welcome", save)

    prompt = _FakeMessage(welcome_handler.WELCOME_PROMPT)
    message = _FakeMessage("Welcome!", reply_to_message=prompt)
    message.reply = AsyncMock()

    await welcome_handler.capture_new_welcome(message)

    message.reply.assert_awaited_once_with(welcome_handler.NOT_ADMIN_TEXT)
    save.assert_not_awaited()


@pytest.mark.asyncio
async def test_capture_new_welcome_saves_and_confirms_for_an_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        welcome_handler, "context_for", AsyncMock(return_value=_FakeCtx(is_admin=True))
    )
    save = AsyncMock()
    monkeypatch.setattr(welcome_handler, "_save_welcome", save)

    prompt = _FakeMessage(welcome_handler.WELCOME_PROMPT)
    bot = type("Bot", (), {"delete_message": AsyncMock()})()
    message = _FakeMessage("Welcome to the crew, <user>!", reply_to_message=prompt, bot=bot)
    message.reply = AsyncMock()
    message.react = AsyncMock()

    await welcome_handler.capture_new_welcome(message)

    save.assert_awaited_once_with(-100, 555, "Welcome to the crew, <user>!")
    message.reply.assert_awaited_once_with(welcome_handler.WELCOME_UPDATED_TEXT)
    bot.delete_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_capture_new_welcome_tolerates_a_reaction_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 👍 reaction is cosmetic (v1 never guards it either) — a failure must
    not block the confirmation reply or the save."""
    monkeypatch.setattr(
        welcome_handler, "context_for", AsyncMock(return_value=_FakeCtx(is_admin=True))
    )
    save = AsyncMock()
    monkeypatch.setattr(welcome_handler, "_save_welcome", save)

    prompt = _FakeMessage(welcome_handler.WELCOME_PROMPT)
    bot = type("Bot", (), {"delete_message": AsyncMock()})()
    message = _FakeMessage("Hi!", reply_to_message=prompt, bot=bot)
    message.reply = AsyncMock()
    message.react = AsyncMock(side_effect=RuntimeError("boom"))

    await welcome_handler.capture_new_welcome(message)

    save.assert_awaited_once()
    message.reply.assert_awaited_once_with(welcome_handler.WELCOME_UPDATED_TEXT)


@pytest.mark.asyncio
async def test_on_join_skips_when_no_joiners(monkeypatch: pytest.MonkeyPatch) -> None:
    context_for = AsyncMock()
    monkeypatch.setattr(welcome_handler, "context_for", context_for)

    message = _FakeMessage(None, new_chat_members=[])
    await welcome_handler.on_join(message)

    context_for.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_join_ignores_the_bot_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    context_for = AsyncMock()
    monkeypatch.setattr(welcome_handler, "context_for", context_for)

    bot = type("Bot", (), {"id": 424242})()
    message = _FakeMessage(None, new_chat_members=[_FakeUser(id=424242)], bot=bot)
    await welcome_handler.on_join(message)

    context_for.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_join_only_processes_the_first_joiner(monkeypatch: pytest.MonkeyPatch) -> None:
    """v1 quirk, preserved: only `new_chat_members[0]` is ever handled — see
    welcome.py's `on_join` docstring and docs/contracts/core_welcome.md."""
    monkeypatch.setattr(
        welcome_handler, "context_for", AsyncMock(return_value=_FakeCtx(is_admin=False))
    )
    fetch = AsyncMock(return_value=None)
    monkeypatch.setattr(welcome_handler, "_fetch_welcome_body", fetch)

    bot = type("Bot", (), {"id": 424242, "send_message": AsyncMock()})()
    first = _FakeUser(id=1, first_name="First", username=None)
    second = _FakeUser(id=2, first_name="Second", username=None)
    message = _FakeMessage(None, new_chat_members=[first, second], bot=bot)
    message.reply = AsyncMock()

    await welcome_handler.on_join(message)

    fetch.assert_awaited_once_with(-100)
    bot.send_message.assert_awaited_once()
    message.reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_join_another_bot_gets_the_bot_participant_notice_not_a_welcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        welcome_handler, "context_for", AsyncMock(return_value=_FakeCtx(is_admin=False))
    )
    fetch = AsyncMock()
    monkeypatch.setattr(welcome_handler, "_fetch_welcome_body", fetch)

    bot = type("Bot", (), {"id": 424242, "send_message": AsyncMock()})()
    other_bot = _FakeUser(id=99, first_name="OtherBot", is_bot=True)
    message = _FakeMessage(None, new_chat_members=[other_bot], bot=bot)
    message.reply = AsyncMock()

    await welcome_handler.on_join(message)

    message.reply.assert_awaited_once()
    fetch.assert_not_awaited()
    bot.send_message.assert_not_awaited()
