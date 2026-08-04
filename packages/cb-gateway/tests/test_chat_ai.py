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

from cb_core.llm.types import Message
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
