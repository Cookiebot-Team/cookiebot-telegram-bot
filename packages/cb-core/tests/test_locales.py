"""v1 locale catalog port.

Backwards compatibility here means byte-for-byte string equality with v1, so the
first class of tests below diffs the copied files against v1's originals directly
(skipped when the reference repo isn't checked out next to this one, e.g. in a
clean clone or CI without the sibling repos). The rest exercise the public API in
`cb_core.locales` without touching v1 at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cb_core import locales

V1_LOCALES = Path(__file__).resolve().parents[3].parent / (
    "COOKIEBOT-Telegram-Group-Bot/Bot/Static/locales"
)
V2_LOCALE_DATA = Path(__file__).resolve().parents[1] / "src/cb_core/locale_data"

# v1's directory name -> v2's canonical language code.
_V1_TO_V2_DIR = {"eng": "en", "pt": "pt", "es": "es"}
_TXT_FILES = (
    "Cookiebot_functions.txt",
    "answers.txt",
    "death.txt",
    "ship_dynamics.txt",
    "sorte.txt",
)


def _require_v1() -> None:
    if not V1_LOCALES.is_dir():
        pytest.skip(f"v1 reference repo not present at {V1_LOCALES}")


class TestByteIdenticalToV1:
    @pytest.mark.parametrize("v1_dir,v2_lang", sorted(_V1_TO_V2_DIR.items()))
    @pytest.mark.parametrize("filename", (*_TXT_FILES, "lib.json"))
    def test_copied_file_matches_v1_byte_for_byte(
        self, v1_dir: str, v2_lang: str, filename: str
    ) -> None:
        _require_v1()
        v1_bytes = (V1_LOCALES / v1_dir / filename).read_bytes()
        v2_bytes = (V2_LOCALE_DATA / v2_lang / filename).read_bytes()
        assert v2_bytes == v1_bytes


class TestResolveLanguage:
    @pytest.mark.parametrize(
        "code,expected",
        [
            ("en", "en"),
            ("eng", "en"),
            ("english", "en"),
            ("en-US", "en"),
            ("pt", "pt"),
            ("pt-BR", "pt"),
            ("pt_br", "pt"),
            ("PT", "pt"),
            ("es", "es"),
            ("es-AR", "es"),
            ("es_ES", "es"),
            (None, "en"),
            ("", "en"),
            ("garbage", "en"),
            ("klingon-XX", "en"),
        ],
    )
    def test_resolves_to_canonical_code(self, code: str | None, expected: str) -> None:
        assert locales.resolve_language(code) == expected

    def test_unlisted_but_related_subtag_falls_back_to_primary(self) -> None:
        # Not explicitly enumerated, but 'pt' is a known primary subtag.
        assert locales.resolve_language("pt-XX") == "pt"


class TestGet:
    def test_substitutes_placeholder(self) -> None:
        text = locales.get("restrict_message", lang="en", time="5")
        assert "5" in text
        assert "%(time)s" not in text

    def test_missing_key_falls_back_to_key_itself(self) -> None:
        assert (
            locales.get("this_key_does_not_exist_anywhere", lang="en")
            == "this_key_does_not_exist_anywhere"
        )

    def test_missing_language_falls_back_to_english_catalog(self) -> None:
        # 'de' isn't a supported language; resolve_language sends it to 'en'
        # already, so this also exercises get() being safe with arbitrary input.
        assert locales.get("canceled", lang="de") == locales.get("canceled", lang="en")

    def test_key_present_in_en_but_missing_in_pt_falls_back(self) -> None:
        # 'teste' is one of the keys known to be absent from pt's lib.json.
        assert locales.get("teste", lang="pt") == locales.catalog("en")["teste"]

    def test_accepts_raw_v1_language_codes(self) -> None:
        assert locales.get("canceled", lang="eng") == locales.get("canceled", lang="en")


class TestCatalog:
    def test_returns_immutable_mapping(self) -> None:
        catalog = locales.catalog("en")
        with pytest.raises(TypeError):
            catalog["new_key"] = "nope"  # type: ignore[index]

    @pytest.mark.parametrize("lang", locales.LANGUAGES)
    def test_every_language_parses_and_is_non_empty(self, lang: str) -> None:
        assert len(locales.catalog(lang)) > 0

    def test_resolves_raw_v1_code(self) -> None:
        assert locales.catalog("eng") == locales.catalog("en")


class TestMissingKeys:
    def test_known_v1_drift_is_asserted_explicitly(self) -> None:
        """v1's lib.json key sets already disagree across languages; this is
        pre-existing data drift (see docs/contracts/locales.md), not something
        the port introduced or should silently paper over.
        """
        missing = locales.missing_keys()
        assert set(missing) == {"pt", "es"}
        assert missing["pt"] == ("caption", "groups", "private_chat", "teste")
        assert missing["es"] == (
            "age",
            "caption",
            "complaint",
            "dice_exemple",
            "dice_roll",
            "groups",
            "private_chat",
            "teste",
        )

    def test_every_missing_key_is_actually_absent(self) -> None:
        en_catalog = locales.catalog("en")
        for lang, keys in locales.missing_keys().items():
            catalog = locales.catalog(lang)
            for key in keys:
                assert key in en_catalog
                assert key not in catalog

    def test_this_bots_own_untranslated_strings_are_each_deliberate(self) -> None:
        """Every `cb.json` key missing from `pt`/`es` answers that group in
        English. Enumerated so adding one is a decision, not an accident.

        Reported separately from `missing_keys()`, which is about v1's
        inherited `lib.json` drift — merging the two made an assertion about
        v1's data change every time this bot added a string.
        """
        missing = locales.missing_cb_keys()

        # v1's own ternary has a `pt` arm and an English `else`, with no
        # Spanish arm at all (`Publisher.py:48`). Preserved as an omission so
        # the gap stays visible — docs/contracts/util_postgetter.md, D-PG-3.
        assert missing["es"] == ("publish_queued", "publish_queued_no_dm", "publisher_ask_prompt")

        # v1 never localises either of these: both are English literals in the
        # source (`Publisher.py:281,285`).
        assert missing["pt"] == ("publish_queued", "publish_queued_no_dm")


class TestLines:
    @pytest.mark.parametrize("lang", locales.LANGUAGES)
    @pytest.mark.parametrize("name", ("death", "sorte", "ship_dynamics", "answers"))
    def test_non_empty_for_every_language(self, lang: str, name: str) -> None:
        result = locales.lines(name, lang=lang)
        assert isinstance(result, tuple)
        assert len(result) > 0
        assert all(line for line in result)

    def test_rejects_a_whole_text_only_file(self) -> None:
        with pytest.raises(ValueError, match="Cookiebot_functions"):
            locales.lines("Cookiebot_functions", lang="en")

    def test_unknown_file_name_raises(self) -> None:
        with pytest.raises(ValueError):
            locales.lines("does_not_exist", lang="en")


class TestText:
    @pytest.mark.parametrize("lang", locales.LANGUAGES)
    def test_commands_help_non_empty_for_every_language(self, lang: str) -> None:
        assert len(locales.text("Cookiebot_functions", lang=lang)) > 0

    def test_unknown_file_name_raises(self) -> None:
        with pytest.raises(ValueError):
            locales.text("does_not_exist", lang="en")


class TestLanguagesTuple:
    def test_is_exactly_the_three_supported_languages(self) -> None:
        assert locales.LANGUAGES == ("en", "pt", "es")
