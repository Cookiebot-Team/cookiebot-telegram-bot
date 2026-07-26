"""Unit tests for core_setlang's pure logic and the setMyCommands side effect.

No Telegram, no database: `derive_join_language`/`parse_manual_commands` are pure
transformations, and `set_group_commands` is exercised against a hand-rolled fake
bot (not aiogram's real client, not our own code — the seam this module owns).
The join handler itself (`on_bot_added_to_group`) and the composed write against a
real `group_configs` row are covered by `qa/test_core_setlang.py` and
`qa/integration/test_setlang.py` respectively.
"""

from __future__ import annotations

import pytest
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand, BotCommandScopeChat

from cb_core import locales
from cb_gateway.handlers import setlang

# ------------------------------------------------------- derive_join_language


class TestDeriveJoinLanguage:
    @pytest.mark.parametrize(
        ("code", "expected"),
        [
            ("pt-BR", "pt"),
            ("pt-br", "pt"),
            ("pt", "pt"),
            ("es-419", "es"),
            ("es-AR", "es"),
            ("es", "es"),
            ("en-GB", "eng"),
            ("en", "eng"),
            ("de", "eng"),
            ("", "eng"),
        ],
    )
    def test_matches_v1s_mapping(self, code: str, expected: str) -> None:
        assert setlang.derive_join_language(code) == expected

    def test_missing_language_code_returns_none(self) -> None:
        """v1 only calls `set_language` when `'language_code' in msg['from']`
        (COOKIEBOT.py:133) — `None` here stands for the key being entirely
        absent, and the caller must leave the group's language untouched."""
        assert setlang.derive_join_language(None) is None

    def test_uppercase_code_does_not_match_either_branch(self) -> None:
        """v1 never lowercases `language_code` before the substring check
        (Configurations.py:243-246) — an uppercase-tagged client falls to the
        `else` branch exactly like v1 would."""
        assert setlang.derive_join_language("PT-BR") == "eng"
        assert setlang.derive_join_language("ES") == "eng"

    def test_naive_substring_match_quirk_is_preserved_not_fixed(self) -> None:
        """v1 checks `'pt' in language_code`, not a leading-subtag match, so any
        code that merely *contains* the two-letter substring anywhere matches —
        including one that plainly is not a Portuguese locale tag. Preserved
        verbatim, per AGENTS.md ("user-visible quirks are usually preserved")."""
        assert setlang.derive_join_language("chapter") == "pt"

    def test_stored_value_is_v1s_literal_not_the_iso_code(self) -> None:
        """The output is v1's own "pt"/"es"/"eng" strings, matching what
        `group_configs.language` actually stores for a v1-created row
        (docs/contracts/group-config.md), not cb_core.locales' canonical
        "en"/"pt"/"es"."""
        assert setlang.derive_join_language("en") == "eng"
        assert setlang.derive_join_language("en") != "en"


# ------------------------------------------------------- parse_manual_commands


class TestParseManualCommands:
    def test_real_english_catalog_yields_real_commands(self) -> None:
        """The confirmed v1 defect (`i18n.get_file` returns a whole string, so
        `for line in lines:` iterates characters and `comandos` is always
        empty) must not be reproduced here — this port fixes it."""
        commands = setlang.parse_manual_commands(locales.text("Cookiebot_functions", "en"))
        assert commands, "expected a non-empty command list"
        by_name = {c.command: c.description for c in commands}
        assert by_name["youtube"] == "Search for a video on youtube"
        assert by_name["configure"] == "Configure the bot"
        assert by_name["privacy"] == "Privacy policy"

    def test_automatic_features_section_is_excluded(self) -> None:
        """ "Publisher - Can use bot to publish..." etc. are capitalized and
        fail `.islower()`, exactly as in v1 (Configurations.py:86)."""
        commands = setlang.parse_manual_commands(locales.text("Cookiebot_functions", "en"))
        names = {c.command for c in commands}
        assert "Publisher" not in names
        assert "publisher" not in names  # the manual command is "publish", not "publisher"
        assert "publish" in names

    def test_multi_word_or_uppercase_left_hand_side_is_excluded(self) -> None:
        assert setlang.parse_manual_commands("Chat AI - Reply to any text message") == []
        assert setlang.parse_manual_commands("Captcha - Stop bots") == []

    def test_lines_without_the_separator_are_ignored(self) -> None:
        assert setlang.parse_manual_commands("Cookiebot Features!\n\nno separator here") == []

    @pytest.mark.parametrize("lang", ["en", "pt", "es"])
    def test_every_locale_produces_a_non_empty_catalog(self, lang: str) -> None:
        commands = setlang.parse_manual_commands(locales.text("Cookiebot_functions", lang))
        assert commands
        assert all(c.command.islower() and len(c.command.split()) == 1 for c in commands)


# --------------------------------------------------------------- set_group_commands


class _FakeBot:
    """Not aiogram's real client, not our own code — the outside-world seam
    `set_group_commands` talks to. Records every call; can be told to reject a
    specific `language_code` scope the way a real Telegram 400 would.
    """

    def __init__(self, *, fail_language_codes: frozenset[str] = frozenset()) -> None:
        self.set_my_commands_calls: list[dict[str, object]] = []
        self.sent_messages: list[dict[str, object]] = []
        self._fail_language_codes = fail_language_codes

    async def set_my_commands(
        self,
        commands: list[BotCommand],
        scope: BotCommandScopeChat | None = None,
        language_code: str | None = None,
    ) -> bool:
        self.set_my_commands_calls.append(
            {"commands": commands, "scope": scope, "language_code": language_code}
        )
        if language_code in self._fail_language_codes:
            raise TelegramAPIError(method=None, message="Bad Request: test failure")
        return True

    async def send_message(self, chat_id: int, text: str, **kwargs: object) -> None:
        self.sent_messages.append({"chat_id": chat_id, "text": text})


class TestSetGroupCommands:
    async def test_relabels_all_three_language_code_scopes(self) -> None:
        bot = _FakeBot()
        ok = await setlang.set_group_commands(bot, -100123, "pt")
        assert ok is True
        codes = {call["language_code"] for call in bot.set_my_commands_calls}
        assert codes == {"pt", "es", "en"}
        for call in bot.set_my_commands_calls:
            assert call["scope"] == BotCommandScopeChat(chat_id=-100123)

    async def test_every_scope_gets_the_target_languages_commands(self) -> None:
        bot = _FakeBot()
        await setlang.set_group_commands(bot, -100123, "pt")
        expected = {
            c.command
            for c in setlang.parse_manual_commands(locales.text("Cookiebot_functions", "pt"))
        }
        for call in bot.set_my_commands_calls:
            assert {c.command for c in call["commands"]} == expected

    async def test_v1_literal_language_resolves_through_cb_core_locales(self) -> None:
        """`language="eng"` (v1's literal stored value) must resolve the same
        catalog as the canonical `"en"` — `set_group_commands` accepts either."""
        bot = _FakeBot()
        await setlang.set_group_commands(bot, -100123, "eng")
        expected = {
            c.command
            for c in setlang.parse_manual_commands(locales.text("Cookiebot_functions", "en"))
        }
        assert {c.command for c in bot.set_my_commands_calls[0]["commands"]} == expected

    async def test_a_rejected_scope_does_not_raise(self) -> None:
        """Failure policy: Telegram rejecting setMyCommands must not fail the
        language change that triggered it."""
        bot = _FakeBot(fail_language_codes=frozenset({"es"}))
        ok = await setlang.set_group_commands(bot, -100123, "pt")
        assert ok is False
        # The other two scopes were still attempted despite the "es" failure.
        codes = {call["language_code"] for call in bot.set_my_commands_calls}
        assert codes == {"pt", "es", "en"}

    async def test_all_scopes_rejected_still_does_not_raise(self) -> None:
        bot = _FakeBot(fail_language_codes=frozenset({"pt", "es", "en"}))
        ok = await setlang.set_group_commands(bot, -100123, "pt")
        assert ok is False

    async def test_confirmation_sent_on_success_when_notify_chat_id_given(self) -> None:
        bot = _FakeBot()
        await setlang.set_group_commands(bot, -100123, "pt", notify_chat_id=999, silent=False)
        assert bot.sent_messages == [
            {
                "chat_id": 999,
                "text": "Comandos no chat com ID <b> -100123 </b> alterados para o idioma <b> Português </b>",
            }
        ]

    async def test_confirmation_text_per_target_language(self) -> None:
        bot = _FakeBot()
        await setlang.set_group_commands(bot, -100123, "es", notify_chat_id=999)
        assert "Español" in bot.sent_messages[-1]["text"]

        bot = _FakeBot()
        await setlang.set_group_commands(bot, -100123, "eng", notify_chat_id=999)
        assert "English" in bot.sent_messages[-1]["text"]

    async def test_no_confirmation_without_a_notify_target(self) -> None:
        bot = _FakeBot()
        await setlang.set_group_commands(bot, -100123, "pt")
        assert bot.sent_messages == []

    async def test_no_confirmation_when_silent(self) -> None:
        bot = _FakeBot()
        await setlang.set_group_commands(bot, -100123, "pt", notify_chat_id=999, silent=True)
        assert bot.sent_messages == []

    async def test_no_confirmation_when_relabeling_failed(self) -> None:
        bot = _FakeBot(fail_language_codes=frozenset({"pt"}))
        await setlang.set_group_commands(bot, -100123, "pt", notify_chat_id=999)
        assert bot.sent_messages == []


# ----------------------------------------------------------------- apply_join_language


class TestApplyJoinLanguage:
    async def test_returns_none_and_writes_nothing_without_a_language_code(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        called = False

        async def boom(*args: object, **kwargs: object) -> None:
            nonlocal called
            called = True

        monkeypatch.setattr(setlang.group_config, "set_config", boom)
        bot = _FakeBot()
        result = await setlang.apply_join_language(bot, -100123, None)
        assert result is None
        assert called is False
        assert bot.set_my_commands_calls == []

    async def test_writes_the_derived_language_and_relabels_commands(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        written: dict[str, object] = {}

        async def fake_set_config(group_id: int, **fields: object) -> None:
            written["group_id"] = group_id
            written["fields"] = fields

        monkeypatch.setattr(setlang.group_config, "set_config", fake_set_config)
        bot = _FakeBot()

        result = await setlang.apply_join_language(bot, -100123, "pt-BR")

        assert result == "pt"
        assert written == {"group_id": -100123, "fields": {"language": "pt"}}
        assert bot.set_my_commands_calls, "expected the command menu to be relabeled too"
