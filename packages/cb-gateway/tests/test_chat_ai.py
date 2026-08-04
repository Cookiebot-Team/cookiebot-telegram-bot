"""Unit tests for x_conversational_ai's pure logic: the mention predicate,
trigger-token stripping, the brevity line and message assembly.

The handler (`cb_gateway.handlers.chat_ai`) does not exist yet -- this file is
written against the module the design specifies (`design.md` R5.4-R5.6, R6)
so the implementation task has a concrete contract to satisfy. Per the same
resolution `packages/cb-gateway/tests/test_battle.py` used for
`.specs/features/fun_battle/tasks.md` T1: every test in this file hangs off
one module-level import, so right now the whole file errors at collection
(`ModuleNotFoundError`) rather than partially passing -- that is the expected,
blessed shape (see `.specs/features/x_conversational_ai/tasks.md` T5's "Done
when"). Once `chat_ai.py` exists with the names below, the pure-logic classes
pass standalone.

What is deliberately NOT here: `reply_with_ai`'s full control flow (the
per-group/per-user/budget gates of R3/R4/R2, the actual model call, the "no
model call when the stripped text is empty" *behaviour*). That needs a real
`ChatContext`, a real cache and a real (or faked) `cb_core.llm.router()`, i.e.
infra this layer does not have -- AGENTS.md SS6 puts that in the acceptance
layer (`qa/`), against mock Telegram, same as every other handler in this
tree. `reply_with_ai`'s *signature* is still pinned here (R5.9), and the pure
decision it must delegate to for the empty-text case
(`stripped_or_placeholder`) is fully covered.

Model: `packages/cb-gateway/tests/test_ship.py` (a pure parsing function, no
Telegram, no database) and `packages/cb-gateway/tests/test_battle.py` (the
same shape, faced the same not-yet-existing-module problem).
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiogram.dispatcher.event.bases import SkipHandler

from cb_core.llm.types import Completion, LLMBudgetExceededError, LLMError, Message, Usage
from cb_gateway.handlers import chat_ai

# ------------------------------------------------------------------ MentionsBot


class TestMentionsBot:
    """R5.4 / D-AI-7: reply-to-a-bot-text always matches; otherwise the
    skin's own display name or `@username`, anywhere in the text,
    case-insensitively. `MentionsBot` takes only already-resolved values --
    no `Message`, no registry lookup -- so it is pure and needs neither
    Telegram nor a database. Those live in the (impure) filter that wraps
    this and is registered on the router per R5.3; that wrapper is the
    handler's job, not this file's.
    """

    def test_reply_to_bot_text_always_matches(self) -> None:
        mentions = chat_ai.MentionsBot(display_name="Cookiebot", bot_username="CookieMWbot")
        assert mentions("anything at all, no trigger word here", reply_to_bot_text="hi!") is True

    def test_reply_to_bot_text_matches_even_with_unrelated_content(self) -> None:
        """The reply-to-bot-text branch does not also require the mention
        text -- v1 evaluates these as independent conditions
        (`COOKIEBOT.py:304`) and R5.4 keeps them independent."""
        mentions = chat_ai.MentionsBot(display_name="Cookiebot", bot_username="CookieMWbot")
        assert mentions("lol what", reply_to_bot_text="some prior answer") is True

    def test_display_name_anywhere_in_text_matches_case_insensitively(self) -> None:
        mentions = chat_ai.MentionsBot(display_name="Cookiebot", bot_username="CookieMWbot")
        assert mentions("hey COOKIEBOT what's up", reply_to_bot_text=None) is True
        assert mentions("hey cookiebot what's up", reply_to_bot_text=None) is True
        assert mentions("hey CoOkIeBoT what's up", reply_to_bot_text=None) is True

    def test_at_username_anywhere_in_text_matches_case_insensitively(self) -> None:
        mentions = chat_ai.MentionsBot(display_name="Cookiebot", bot_username="CookieMWbot")
        assert mentions("yo @CookieMWbot answer this", reply_to_bot_text=None) is True
        assert mentions("yo @cookiemwbot answer this", reply_to_bot_text=None) is True
        assert mentions("yo @COOKIEMWBOT answer this", reply_to_bot_text=None) is True

    def test_neither_present_is_no_match(self) -> None:
        mentions = chat_ai.MentionsBot(display_name="Cookiebot", bot_username="CookieMWbot")
        assert mentions("just a normal message", reply_to_bot_text=None) is False

    def test_bare_username_without_at_sign_does_not_match(self) -> None:
        """v1's own trigger checks the literal `"@CookieMWbot"` substring,
        `@` included (`COOKIEBOT.py:304`) -- a bare mention of the username
        with no `@` is not a trigger."""
        mentions = chat_ai.MentionsBot(display_name="Cookiebot", bot_username="CookieMWbot")
        assert mentions("CookieMWbot without an at sign", reply_to_bot_text=None) is False

    def test_driven_by_the_skin_not_a_hardcoded_literal(self) -> None:
        """D-AI-7: v1 hardcodes `"cookiebot"`/`"@CookieMWbot"`
        (`COOKIEBOT.py:304`) so a second skin like `bombot` could never be
        addressed by its own name. `MentionsBot` takes the skin's own
        display name and username, so a non-cookiebot skin is matched on its
        own identity and *not* on v1's literals."""
        pawstral = chat_ai.MentionsBot(display_name="PawstralBot", bot_username="pawstralbot")
        assert pawstral("yo pawstralbot answer me", reply_to_bot_text=None) is True

        # The same text means nothing to a differently-skinned bot -- no
        # hardcoded "cookiebot"/"@CookieMWbot" literal is hiding in there.
        cookiebot = chat_ai.MentionsBot(display_name="Cookiebot", bot_username="CookieMWbot")
        assert cookiebot("yo pawstralbot answer me", reply_to_bot_text=None) is False


# ------------------------------------------------------------ trigger stripping


class TestStripTriggerTokens:
    """R5.5, D-AI-3, D-AI-7: strip the same tokens the filter matched on
    (case-insensitively), turn newlines into spaces, `.strip()` -- and
    critically, no `.capitalize()`."""

    def test_removes_the_display_name_case_insensitively(self) -> None:
        stripped = chat_ai.strip_trigger_tokens(
            "Hey COOKIEBOT what's up", display_name="Cookiebot", bot_username="CookieMWbot"
        )
        assert stripped == "Hey  what's up"

    def test_removes_the_at_username_case_insensitively(self) -> None:
        stripped = chat_ai.strip_trigger_tokens(
            "yo @CookieMWBOT answer this", display_name="Cookiebot", bot_username="CookieMWbot"
        )
        assert stripped == "yo  answer this"

    def test_newlines_become_spaces(self) -> None:
        stripped = chat_ai.strip_trigger_tokens(
            "Cookiebot\nhello\nworld", display_name="Cookiebot", bot_username="CookieMWbot"
        )
        assert stripped == "hello world"

    def test_consecutive_newlines_are_not_collapsed(self) -> None:
        """`.replace("\\n", " ")` is one-for-one, same as v1
        (`NaturalLanguage.py:71`) -- no whitespace collapsing was ever part
        of the contract, so a double blank line becomes two spaces, not one."""
        stripped = chat_ai.strip_trigger_tokens(
            "Cookiebot hi\n\nthere", display_name="Cookiebot", bot_username="CookieMWbot"
        )
        assert stripped == "hi  there"

    def test_no_capitalize(self) -> None:
        """D-AI-3: v1's `.capitalize()` (`NaturalLanguage.py:69-70`) lowercases
        every character after the first, wrecking acronyms and proper nouns.
        Dropped -- the leftover text is untouched casing."""
        stripped = chat_ai.strip_trigger_tokens(
            "cookiebot what is up", display_name="Cookiebot", bot_username="CookieMWbot"
        )
        assert stripped == "what is up"  # NOT "What is up" -- .capitalize() is gone

    def test_untriggered_text_only_gets_newline_and_strip_treatment(self) -> None:
        stripped = chat_ai.strip_trigger_tokens(
            "hello\nworld", display_name="Cookiebot", bot_username="CookieMWbot"
        )
        assert stripped == "hello world"

    def test_leading_and_trailing_whitespace_is_stripped(self) -> None:
        stripped = chat_ai.strip_trigger_tokens(
            "  Cookiebot hello  ", display_name="Cookiebot", bot_username="CookieMWbot"
        )
        assert stripped == "hello"


class TestStrippedOrPlaceholder:
    """R5.5 / v1 `NaturalLanguage.py:74`: an empty result after stripping
    becomes the literal `"?"`. This is the pure decision `reply_with_ai` must
    act on to skip the model call entirely -- the skip itself is control flow
    that belongs to the handler (see this file's module docstring)."""

    def test_empty_after_stripping_is_a_literal_question_mark(self) -> None:
        result = chat_ai.stripped_or_placeholder(
            "Cookiebot", display_name="Cookiebot", bot_username="CookieMWbot"
        )
        assert result == "?"

    def test_whitespace_only_is_also_a_literal_question_mark(self) -> None:
        result = chat_ai.stripped_or_placeholder(
            "   ", display_name="Cookiebot", bot_username="CookieMWbot"
        )
        assert result == "?"

    def test_only_the_at_username_is_also_empty(self) -> None:
        result = chat_ai.stripped_or_placeholder(
            "@CookieMWbot", display_name="Cookiebot", bot_username="CookieMWbot"
        )
        assert result == "?"

    def test_non_empty_text_passes_through_unchanged(self) -> None:
        result = chat_ai.stripped_or_placeholder(
            "Cookiebot what time is it", display_name="Cookiebot", bot_username="CookieMWbot"
        )
        assert result == "what time is it"


# ----------------------------------------------------------------- brevity line


class TestBrevityLine:
    """R5.6, verbatim from v1 `NaturalLanguage.py:26-31` for the three known
    languages; any other language appends nothing at all (v1's stray
    `"\\n\\n"` is dropped, not ported)."""

    def test_en(self) -> None:
        assert chat_ai.brevity_line("en") == "Try to reduce the answer a lot."

    def test_pt(self) -> None:
        assert chat_ai.brevity_line("pt") == "Tente reduzir bastante a resposta."

    def test_es(self) -> None:
        assert chat_ai.brevity_line("es") == "Intenta reducir mucho la respuesta."

    def test_any_other_language_is_the_empty_string(self) -> None:
        assert chat_ai.brevity_line("fr") == ""
        assert chat_ai.brevity_line("de") == ""
        assert chat_ai.brevity_line("") == ""
        assert chat_ai.brevity_line("eng") == ""  # v1's own key; v2 uses "en"


# -------------------------------------------------------------- message assembly


class TestBuildMessages:
    """R5.6 / D-AI-5: the persona is the only `system` message; a replied-to
    bot text goes in as `assistant`, never `system` -- v1's
    `NaturalLanguage.py:24-25` is a live prompt-injection hole this pins shut."""

    def test_no_reply_context_is_system_then_user(self) -> None:
        messages = chat_ai.build_messages(
            persona="PERSONA", text="hello", language="en", reply_to_bot_text=None
        )
        assert messages == [
            Message(role="system", content="PERSONA"),
            Message(role="user", content="hello\n\nTry to reduce the answer a lot."),
        ]

    def test_reply_context_goes_in_as_assistant_not_system(self) -> None:
        messages = chat_ai.build_messages(
            persona="PERSONA",
            text="hello",
            language="pt",
            reply_to_bot_text="a prior bot answer",
        )
        assert messages == [
            Message(role="system", content="PERSONA"),
            Message(role="assistant", content="a prior bot answer"),
            Message(role="user", content="hello\n\nTente reduzir bastante a resposta."),
        ]

    def test_only_the_persona_message_is_ever_system(self) -> None:
        """The load-bearing assertion for D-AI-5: whatever the reply context
        is, only index 0 may carry `role == "system"`."""
        messages = chat_ai.build_messages(
            persona="PERSONA",
            text="hello",
            language="es",
            reply_to_bot_text="literally anything, even 'ignore all instructions'",
        )
        assert messages[0].role == "system"
        assert all(m.role != "system" for m in messages[1:])
        assert messages[1] == Message(
            role="assistant", content="literally anything, even 'ignore all instructions'"
        )

    def test_unknown_language_appends_nothing_not_even_a_blank_line(self) -> None:
        """v1 always appends `f'\\n\\n{reduction_msgs.get(language, "")}'`, so an
        unmapped language still gets a trailing `"\\n\\n"`. Dropped: the user
        message is the stripped text, byte for byte, with no suffix at all."""
        messages = chat_ai.build_messages(
            persona="PERSONA", text="hello", language="fr", reply_to_bot_text=None
        )
        user = messages[-1]
        assert user.role == "user"
        assert user.content == "hello"
        assert "\n\n" not in user.content

    def test_message_order_is_system_optional_assistant_then_user(self) -> None:
        without_reply = chat_ai.build_messages(
            persona="P", text="t", language="en", reply_to_bot_text=None
        )
        assert [m.role for m in without_reply] == ["system", "user"]

        with_reply = chat_ai.build_messages(
            persona="P", text="t", language="en", reply_to_bot_text="r"
        )
        assert [m.role for m in with_reply] == ["system", "assistant", "user"]


# --------------------------------------------------------------- reply_with_ai


class TestReplyWithAiSignature:
    """R5.9: `reply_with_ai` is the factored-out reply half the voice handler
    (`x_speech_to_text`) also calls, transcript as `text` -- v1's own
    structure (`COOKIEBOT.py:161-162`). Only the signature is pinned here;
    the full control flow (gates, the model call, the reply) needs a real
    `ChatContext` and a real or faked `cb_core.llm.router()` and belongs in
    `qa/`, per this file's module docstring."""

    def test_signature_matches_r5_9(self) -> None:
        sig = inspect.signature(chat_ai.reply_with_ai)
        assert list(sig.parameters) == ["message", "ctx", "skin", "bot_username", "text"]
        for name in ("skin", "bot_username", "text"):
            assert sig.parameters[name].kind == inspect.Parameter.KEYWORD_ONLY
        for name in ("message", "ctx"):
            assert sig.parameters[name].kind in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            )

    def test_is_a_coroutine_function(self) -> None:
        assert inspect.iscoroutinefunction(chat_ai.reply_with_ai)


# ------------------------------------------------------------------------
# Handler-level tests: everything the pure-function tests above cannot
# reach on their own -- the three gates in `ai_reply` (R5.7), the
# no-model-call short-circuit inside `reply_with_ai` (R5.5) and every
# failure path replying (D-AI-4/R5.10). Same monkeypatch-the-module-global
# style `test_stickerspam.py`/`test_doomlist.py` use: no Telegram, no real
# Valkey, no real router -- every collaborator `chat_ai` reaches for is a
# module-level name this file swaps out.
# ------------------------------------------------------------------------


@dataclass
class _FakeCtx:
    """Duck-typed `ChatContext` -- `ai_reply`/`reply_with_ai` read only
    `.group_id` and `.lang`, so there is no need to construct a real one
    (which would need a `GroupConfig` and an `ActorCheck`)."""

    group_id: int
    lang: str = "en"


@dataclass
class _FakeTenant:
    tenant_id: str = "cookiebot"
    display_name: str = "Cookiebot"


@dataclass
class _FakeChat:
    id: int


@dataclass
class _FakeUser:
    id: int


@dataclass
class _FakeMessage:
    """Only the attributes the handler actually reads -- `_FakeMessage.bot`
    carries just `.id` and `.send_chat_action`, both of which the real
    `aiogram.Bot` also has."""

    chat: _FakeChat
    from_user: _FakeUser | None
    text: str | None
    reply_to_message: Any = None
    message_id: int = 1
    bot: Any = None
    reply: Any = field(default=None)


def _bot(bot_id: int = 999) -> Any:
    return SimpleNamespace(id=bot_id, send_chat_action=AsyncMock())


def _message(
    text: str | None = "hey Cookiebot",
    *,
    user_id: int | None = 1,
    group_id: int = -100,
    reply_to_message: Any = None,
    bot_id: int = 999,
) -> _FakeMessage:
    message = _FakeMessage(
        chat=_FakeChat(id=group_id),
        from_user=_FakeUser(id=user_id) if user_id is not None else None,
        text=text,
        reply_to_message=reply_to_message,
        bot=_bot(bot_id),
    )
    message.reply = AsyncMock()
    return message


def _settings(*, limit: int = 20, window: int = 60) -> Any:
    return SimpleNamespace(ai_chat_group_limit=limit, ai_chat_window_seconds=window)


# ------------------------------------------------------------- ai_reply gates


class TestAiReplyGateOrder:
    """R5.7: per-group window (R3), then the per-user streak (R4) -- the
    budget check (R2) lives inside `reply_with_ai`'s router call and is
    covered separately by `TestReplyWithAiFailurePaths`."""

    async def test_group_limit_hit_exactly_replies_and_stops(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(chat_ai, "context_for", AsyncMock(return_value=_FakeCtx(group_id=-100)))
        monkeypatch.setattr(chat_ai, "get_settings", lambda: _settings(limit=20))
        monkeypatch.setattr(chat_ai, "_bump_group", AsyncMock(return_value=20))
        spend = AsyncMock()
        monkeypatch.setattr(chat_ai, "_spend_streak", spend)
        reply_with_ai = AsyncMock()
        monkeypatch.setattr(chat_ai, "reply_with_ai", reply_with_ai)

        from cb_core import locales

        message = _message()
        await chat_ai.ai_reply(message)

        message.reply.assert_awaited_once_with(locales.get("ai_rate_limited", "en"))
        spend.assert_not_awaited()
        reply_with_ai.assert_not_awaited()

    async def test_group_limit_exceeded_is_silent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(chat_ai, "context_for", AsyncMock(return_value=_FakeCtx(group_id=-100)))
        monkeypatch.setattr(chat_ai, "get_settings", lambda: _settings(limit=20))
        monkeypatch.setattr(chat_ai, "_bump_group", AsyncMock(return_value=27))
        spend = AsyncMock()
        monkeypatch.setattr(chat_ai, "_spend_streak", spend)
        reply_with_ai = AsyncMock()
        monkeypatch.setattr(chat_ai, "reply_with_ai", reply_with_ai)

        message = _message()
        await chat_ai.ai_reply(message)

        message.reply.assert_not_awaited()
        spend.assert_not_awaited()
        reply_with_ai.assert_not_awaited()

    async def test_streak_exhausted_after_the_group_gate_passes_is_silent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The seventh consecutive trigger is answered with nothing at all
        (R4.3) -- but only once the (cheaper, more local) group gate has
        already passed."""
        bump_group = AsyncMock(return_value=1)
        monkeypatch.setattr(chat_ai, "context_for", AsyncMock(return_value=_FakeCtx(group_id=-100)))
        monkeypatch.setattr(chat_ai, "get_settings", lambda: _settings(limit=20))
        monkeypatch.setattr(chat_ai, "_bump_group", bump_group)
        monkeypatch.setattr(chat_ai, "_spend_streak", AsyncMock(return_value=0))
        reply_with_ai = AsyncMock()
        monkeypatch.setattr(chat_ai, "reply_with_ai", reply_with_ai)

        message = _message()
        await chat_ai.ai_reply(message)

        bump_group.assert_awaited_once()
        message.reply.assert_not_awaited()
        reply_with_ai.assert_not_awaited()

    async def test_gate_order_is_group_then_streak_then_reply(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []

        async def fake_bump_group(group_id: int, window_seconds: int) -> int:
            calls.append("group")
            return 1

        async def fake_spend_streak(user_id: int) -> int:
            calls.append("streak")
            return 6

        async def fake_reply_with_ai(
            message: _FakeMessage, ctx: _FakeCtx, *, skin: str, bot_username: str, text: str
        ) -> None:
            calls.append("reply")

        monkeypatch.setattr(chat_ai, "context_for", AsyncMock(return_value=_FakeCtx(group_id=-100)))
        monkeypatch.setattr(chat_ai, "get_settings", lambda: _settings())
        monkeypatch.setattr(chat_ai, "_bump_group", fake_bump_group)
        monkeypatch.setattr(chat_ai, "_spend_streak", fake_spend_streak)
        monkeypatch.setattr(chat_ai, "reply_with_ai", fake_reply_with_ai)

        await chat_ai.ai_reply(_message())

        assert calls == ["group", "streak", "reply"]

    async def test_both_gates_fail_open_on_a_cache_outage(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(chat_ai, "context_for", AsyncMock(return_value=_FakeCtx(group_id=-100)))
        monkeypatch.setattr(chat_ai, "get_settings", lambda: _settings())
        monkeypatch.setattr(chat_ai, "_bump_group", AsyncMock(return_value=None))
        monkeypatch.setattr(chat_ai, "_spend_streak", AsyncMock(return_value=None))
        reply_with_ai = AsyncMock()
        monkeypatch.setattr(chat_ai, "reply_with_ai", reply_with_ai)

        message = _message()
        await chat_ai.ai_reply(message)

        reply_with_ai.assert_awaited_once()

    async def test_no_from_user_skips_the_streak_gate_entirely(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(chat_ai, "context_for", AsyncMock(return_value=_FakeCtx(group_id=-100)))
        monkeypatch.setattr(chat_ai, "get_settings", lambda: _settings())
        monkeypatch.setattr(chat_ai, "_bump_group", AsyncMock(return_value=1))
        spend = AsyncMock()
        monkeypatch.setattr(chat_ai, "_spend_streak", spend)
        reply_with_ai = AsyncMock()
        monkeypatch.setattr(chat_ai, "reply_with_ai", reply_with_ai)

        message = _message(user_id=None)
        await chat_ai.ai_reply(message)

        spend.assert_not_awaited()
        reply_with_ai.assert_awaited_once()


class TestAiReplyEndToEnd:
    """The "done when" bar from `tasks.md` T6, proven at the unit layer: a
    mention that clears every gate gets a real reply."""

    async def test_a_mention_gets_a_reply(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(chat_ai, "context_for", AsyncMock(return_value=_FakeCtx(group_id=-100)))
        monkeypatch.setattr(chat_ai, "get_settings", lambda: _settings())
        monkeypatch.setattr(chat_ai, "_bump_group", AsyncMock(return_value=1))
        monkeypatch.setattr(chat_ai, "_spend_streak", AsyncMock(return_value=6))
        monkeypatch.setattr(
            chat_ai.tenancy.registry, "by_skin", AsyncMock(return_value=_FakeTenant())
        )
        completion = Completion(text="hi there", model="m", provider="p", usage=Usage())
        fake_router = SimpleNamespace(complete=AsyncMock(return_value=completion))
        monkeypatch.setattr(chat_ai, "llm_router", lambda: fake_router)

        message = _message(text="hey Cookiebot, what's up?")
        await chat_ai.ai_reply(message, skin="cookiebot", bot_username="CookieMWbot")

        message.reply.assert_awaited_once_with("hi there")
        fake_router.complete.assert_awaited_once()


# ------------------------------------------------------------ reply_with_ai body


class TestReplyWithAiNoModelCallWhenEmpty:
    async def test_empty_stripped_text_answers_a_placeholder_with_no_model_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            chat_ai.tenancy.registry, "by_skin", AsyncMock(return_value=_FakeTenant())
        )
        fake_router = SimpleNamespace(complete=AsyncMock())
        monkeypatch.setattr(chat_ai, "llm_router", lambda: fake_router)

        message = _message(text="Cookiebot")
        ctx = _FakeCtx(group_id=-100)

        await chat_ai.reply_with_ai(
            message, ctx, skin="cookiebot", bot_username="CookieMWbot", text="Cookiebot"
        )

        message.reply.assert_awaited_once_with("?")
        fake_router.complete.assert_not_awaited()
        message.bot.send_chat_action.assert_awaited_once()


class TestReplyWithAiFailurePaths:
    """D-AI-4/R5.10: every failure path produces a visible reply -- v1 only
    caught three OpenAI exception types and left everything else, timeouts
    included, with no reply at all."""

    async def test_budget_exceeded_replies_with_quota_spent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            chat_ai.tenancy.registry, "by_skin", AsyncMock(return_value=_FakeTenant())
        )
        error = LLMBudgetExceededError("cookiebot", 10.0, 5.0)
        fake_router = SimpleNamespace(complete=AsyncMock(side_effect=error))
        monkeypatch.setattr(chat_ai, "llm_router", lambda: fake_router)

        message = _message(text="Cookiebot hi there")
        ctx = _FakeCtx(group_id=-100)
        await chat_ai.reply_with_ai(
            message, ctx, skin="cookiebot", bot_username="CookieMWbot", text="Cookiebot hi there"
        )

        from cb_core import locales

        message.reply.assert_awaited_once_with(locales.get("ai_quota_spent", "en"))

    async def test_llm_error_replies_with_ai_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            chat_ai.tenancy.registry, "by_skin", AsyncMock(return_value=_FakeTenant())
        )
        fake_router = SimpleNamespace(complete=AsyncMock(side_effect=LLMError("provider down")))
        monkeypatch.setattr(chat_ai, "llm_router", lambda: fake_router)

        message = _message(text="Cookiebot hi there")
        ctx = _FakeCtx(group_id=-100)
        await chat_ai.reply_with_ai(
            message, ctx, skin="cookiebot", bot_username="CookieMWbot", text="Cookiebot hi there"
        )

        from cb_core import locales

        message.reply.assert_awaited_once_with(locales.get("ai_unavailable", "en"))

    async def test_unexpected_exception_also_replies_with_ai_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """D-AI-4's whole point: v1's bare `except (RateLimitError,
        APIConnectionError, APIStatusError)` let a timeout or anything else
        escape with no reply. Nothing here is that narrow."""
        monkeypatch.setattr(
            chat_ai.tenancy.registry, "by_skin", AsyncMock(return_value=_FakeTenant())
        )
        fake_router = SimpleNamespace(complete=AsyncMock(side_effect=TimeoutError("timed out")))
        monkeypatch.setattr(chat_ai, "llm_router", lambda: fake_router)

        message = _message(text="Cookiebot hi there")
        ctx = _FakeCtx(group_id=-100)
        await chat_ai.reply_with_ai(
            message, ctx, skin="cookiebot", bot_username="CookieMWbot", text="Cookiebot hi there"
        )

        from cb_core import locales

        message.reply.assert_awaited_once_with(locales.get("ai_unavailable", "en"))

    async def test_success_replies_with_the_completion_text_untouched(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """R6.4: no jailbreak split, no regex laundering -- the model's text
        goes out exactly as written."""
        monkeypatch.setattr(
            chat_ai.tenancy.registry, "by_skin", AsyncMock(return_value=_FakeTenant())
        )
        completion = Completion(
            text="A Perfectly Cased Answer, ACRONYM intact.", model="m", provider="p", usage=Usage()
        )
        fake_router = SimpleNamespace(complete=AsyncMock(return_value=completion))
        monkeypatch.setattr(chat_ai, "llm_router", lambda: fake_router)

        message = _message(text="Cookiebot hi there")
        ctx = _FakeCtx(group_id=-100)
        await chat_ai.reply_with_ai(
            message, ctx, skin="cookiebot", bot_username="CookieMWbot", text="Cookiebot hi there"
        )

        message.reply.assert_awaited_once_with("A Perfectly Cased Answer, ACRONYM intact.")

    async def test_reply_context_reaches_the_model_as_assistant_not_system(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end version of D-AI-5's pin: a reply-to-bot text must
        arrive at the router as an `assistant` message, never `system`."""
        monkeypatch.setattr(
            chat_ai.tenancy.registry, "by_skin", AsyncMock(return_value=_FakeTenant())
        )
        completion = Completion(text="ok", model="m", provider="p", usage=Usage())
        fake_complete = AsyncMock(return_value=completion)
        fake_router = SimpleNamespace(complete=fake_complete)
        monkeypatch.setattr(chat_ai, "llm_router", lambda: fake_router)

        reply_to = SimpleNamespace(from_user=_FakeUser(id=999), text="a prior bot answer")
        message = _message(text="Cookiebot and?", reply_to_message=reply_to, bot_id=999)
        ctx = _FakeCtx(group_id=-100)
        await chat_ai.reply_with_ai(
            message, ctx, skin="cookiebot", bot_username="CookieMWbot", text="Cookiebot and?"
        )

        args, _kwargs = fake_complete.await_args
        _task, sent_messages = args
        assert sent_messages[0].role == "system"
        assert all(m.role != "system" for m in sent_messages[1:])
        assert Message(role="assistant", content="a prior bot answer") in sent_messages


# --------------------------------------------------------------- replenish


class TestReplenishHandler:
    """R5.3(2)/R4.4: any group text message that reaches this router without
    triggering `ai_reply` replenishes the streak, then yields -- v1's `else`
    (`COOKIEBOT.py:313`)."""

    async def test_replenishes_the_streak_and_yields(self, monkeypatch: pytest.MonkeyPatch) -> None:
        replenish_mock = AsyncMock()
        monkeypatch.setattr(chat_ai, "_replenish_streak", replenish_mock)

        message = _message(user_id=42)
        with pytest.raises(SkipHandler):
            await chat_ai.replenish(message)

        replenish_mock.assert_awaited_once_with(42)

    async def test_no_from_user_still_yields_without_replenishing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        replenish_mock = AsyncMock()
        monkeypatch.setattr(chat_ai, "_replenish_streak", replenish_mock)

        message = _message(user_id=None)
        with pytest.raises(SkipHandler):
            await chat_ai.replenish(message)

        replenish_mock.assert_not_awaited()
