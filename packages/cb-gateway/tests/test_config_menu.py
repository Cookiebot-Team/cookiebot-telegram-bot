"""Unit tests for util_config's pure logic: callback-data parsing and menu shape.

The write path itself (`group_config.set_config`) is exercised against a real
database in `qa/integration/test_config_menu.py`; the acceptance flow (permission
gating, the anonymous-admin fix, callback answering) lives in
`qa/test_util_config.py`. This file is everything in between — no Telegram, no
database, just the transformations a v1 parity bug hides in.
"""

from __future__ import annotations

import dataclasses

import pytest

from cb_core.group_config import DEFAULTS
from cb_gateway.handlers import config_menu as cm

GROUP_ID = -1001234567890


# ------------------------------------------------------------- CONFIG_FIELDS


class TestConfigFieldsCatalog:
    def test_letters_and_order_match_v1_exactly(self) -> None:
        """`configurar`'s inline_keyboard order, `Configurations.py:150-163`."""
        assert [f.letter for f in cm.CONFIG_FIELDS] == [
            "k",
            "a",
            "b",
            "c",
            "d",
            "h",
            "i",
            "j",
            "m",
            "n",
            "o",
            "p",
            "q",
        ]

    def test_labels_match_v1_exactly(self) -> None:
        assert [f.label for f in cm.CONFIG_FIELDS] == [
            "Language",
            "FurBots",
            "Stickers limit",
            "🕒 Limbo",
            "🕒 CAPTCHA",
            "Fun Functions",
            "Utility Functions",
            "SFW Chat",
            "Publisher Post",
            "Publisher Ask",
            "Thread Posts",
            "Max Posts",
            "Publisher Members Only",
        ]

    def test_every_field_maps_to_a_real_group_config_column(self) -> None:
        columns = {f.name for f in dataclasses.fields(DEFAULTS)}
        for field in cm.CONFIG_FIELDS:
            assert field.column in columns, field.column

    def test_no_duplicate_letters(self) -> None:
        letters = [f.letter for f in cm.CONFIG_FIELDS]
        assert len(letters) == len(set(letters))


# --------------------------------------------------------- callback data wire


class TestCallbackData:
    @pytest.mark.parametrize("letter", [f.letter for f in cm.CONFIG_FIELDS])
    def test_round_trips(self, letter: str) -> None:
        data = cm.build_callback_data(letter, GROUP_ID)
        assert data == f"{letter} CONFIG {GROUP_ID}"
        assert cm.parse_callback_data(data) == (letter, GROUP_ID)

    def test_positive_group_id(self) -> None:
        assert cm.parse_callback_data("a CONFIG 12345") == ("a", 12345)

    @pytest.mark.parametrize(
        "data",
        [
            "",
            "garbage",
            "a CONFIG",
            "a CONFIG notanumber",
            "a CONFIG 1 extra",
            "z CONFIG -100123",  # unknown letter
            "a WRONGTOKEN -100123",
        ],
    )
    def test_malformed_or_unknown_is_none(self, data: str) -> None:
        assert cm.parse_callback_data(data) is None


class TestMenuKeyboard:
    def test_one_button_per_row_in_field_order(self) -> None:
        markup = cm.build_menu_keyboard(GROUP_ID)
        assert len(markup.inline_keyboard) == len(cm.CONFIG_FIELDS)
        for row, field in zip(markup.inline_keyboard, cm.CONFIG_FIELDS, strict=True):
            assert len(row) == 1
            assert row[0].text == field.label
            assert row[0].callback_data == cm.build_callback_data(field.letter, GROUP_ID)


class TestPromptRoundTrip:
    def test_prompt_carries_the_group_id_and_marker(self) -> None:
        field = cm.FIELD_BY_LETTER["h"]
        prompt = cm.build_prompt(field, GROUP_ID)
        assert prompt.startswith(f"Chat = {GROUP_ID}\n")
        assert field.prompt in prompt
        assert "REPLY THIS MESSAGE with the new variable value" in prompt

    def test_extract_group_id_recovers_a_negative_id(self) -> None:
        prompt = cm.build_prompt(cm.FIELD_BY_LETTER["a"], GROUP_ID)
        assert cm.extract_group_id(prompt) == GROUP_ID

    @pytest.mark.parametrize("text", ["", "no chat line here", "Chat = notanumber\nfoo"])
    def test_extract_group_id_rejects_malformed_text(self, text: str) -> None:
        assert cm.extract_group_id(text) is None

    def test_find_field_by_prompt_matches_the_right_field(self) -> None:
        for field in cm.CONFIG_FIELDS:
            prompt = cm.build_prompt(field, GROUP_ID)
            assert cm.find_field_by_prompt(prompt) is field

    def test_find_field_by_prompt_none_for_unrelated_text(self) -> None:
        assert cm.find_field_by_prompt("just a regular reply, not a config prompt") is None


# --------------------------------------------------------------- reply parsing


class TestParseReplyValue:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("1", True), ("0", False), ("2", True), ("  1  ", True)],
    )
    def test_bool_field(self, raw: str, expected: bool) -> None:
        field = cm.FIELD_BY_LETTER["h"]  # functions_fun
        assert cm.parse_reply_value(field, raw) is expected

    @pytest.mark.parametrize("raw", ["", "yes", "true", "1.5"])
    def test_bool_field_rejects_non_numeric(self, raw: str) -> None:
        field = cm.FIELD_BY_LETTER["h"]
        assert cm.parse_reply_value(field, raw) is None

    def test_int_field(self) -> None:
        field = cm.FIELD_BY_LETTER["p"]  # max_posts
        assert cm.parse_reply_value(field, "42") == 42
        assert cm.parse_reply_value(field, "-3") == -3

    def test_int_field_rejects_non_numeric(self) -> None:
        field = cm.FIELD_BY_LETTER["p"]
        assert cm.parse_reply_value(field, "not-a-number") is None

    def test_topic_field_stores_a_string(self) -> None:
        field = cm.FIELD_BY_LETTER["o"]  # thread_posts
        value = cm.parse_reply_value(field, "17")
        assert value == "17"
        assert isinstance(value, str)

    def test_topic_field_rejects_non_numeric(self) -> None:
        field = cm.FIELD_BY_LETTER["o"]
        assert cm.parse_reply_value(field, "abc") is None

    def test_language_field_accepts_any_non_empty_string(self) -> None:
        """v1 does not validate this beyond non-empty either — see docstring."""
        field = cm.FIELD_BY_LETTER["k"]
        assert cm.parse_reply_value(field, "eng") == "eng"
        assert cm.parse_reply_value(field, "pt") == "pt"
        assert cm.parse_reply_value(field, "klingon") == "klingon"

    def test_language_field_rejects_empty(self) -> None:
        field = cm.FIELD_BY_LETTER["k"]
        assert cm.parse_reply_value(field, "") is None
        assert cm.parse_reply_value(field, "   ") is None


# ------------------------------------------------------------------- menu text


class TestMenuText:
    def test_current_settings_block_matches_v1_layout(self) -> None:
        config = dataclasses.replace(DEFAULTS, group_id=GROUP_ID)
        text = cm.menu_text(config)
        assert text.startswith("Current settings:\n\n")
        assert "FurBots: True" in text
        assert "\n sfw: True" in text
        assert "Choose the variable you would like to change" in text
        assert "/newrules or /newwelcome" in text

    def test_publisher_members_only_is_not_in_the_summary(self) -> None:
        """v1's `variables` string stops at Max Posts — reproduced, not fixed."""
        config = dataclasses.replace(DEFAULTS, group_id=GROUP_ID, publisher_members_only=True)
        text = cm.menu_text(config)
        assert "Publisher Members Only" not in text


class TestWriteFailure:
    """v1 crashed on a failed write and told the admin nothing.

    `group_config.set_config` deliberately raises on a database failure instead of
    degrading the way the read path does — a write that silently does nothing is
    worse than an error. That only helps if the handler says so.
    """

    async def test_admin_is_told_when_the_write_does_not_land(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cb_gateway.handlers import config_menu as mod

        async def boom(*args: object, **kwargs: object) -> None:
            raise RuntimeError("pg pool not initialised")

        monkeypatch.setattr(mod, "apply_change", boom)

        sent: list[str] = []

        field = next(f for f in mod.CONFIG_FIELDS if f.column == "max_posts")

        class _Reply:
            text = f"Chat = -100123\n{field.prompt}\nREPLY THIS MESSAGE with the new variable value"

        class _Message:
            reply_to_message = _Reply()
            text = "42"

            async def reply(self, body: str, **_: object) -> None:
                sent.append(body)

            async def answer(self, body: str, **_: object) -> None:
                sent.append(body)

            async def react(self, *_args: object, **_kw: object) -> None:
                raise AssertionError("must not confirm a write that failed")

        await mod.apply_config_reply(_Message())

        assert sent, "the admin was told nothing"
        assert "could not save" in sent[-1]
        assert "Successfully changed" not in " ".join(sent)
