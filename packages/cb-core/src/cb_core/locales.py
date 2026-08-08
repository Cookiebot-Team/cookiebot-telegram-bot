"""v1 string catalog, ported verbatim.

v1 (`../COOKIEBOT-Telegram-Group-Bot/Bot/loc.py`) kept every user-facing string in
flat files under `Bot/Static/locales/{eng,pt,es}/` and read them at request time
with an lru_cache'd `Localizer`. Groups are live on v1 today, so the strings here
must match byte for byte — this module is a data port, not a rewrite of copy. A
Postgres override layer for tenant branding is planned on top of this, not
instead of it (see docs/contracts/locales.md).

Everything is loaded once at import into immutable structures so there is no I/O,
no cache invalidation and no global mutable state to reason about on the request
path — the whole point of v1's per-process, un-invalidated dict cache (FEATURE-MAP
D6) is exactly what this replaces.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from importlib import resources
from types import MappingProxyType

from cb_core import metrics

LANGUAGES: tuple[str, ...] = ("en", "pt", "es")

_DEFAULT_LANGUAGE = "en"
_DATA_PACKAGE = "cb_core.locale_data"

# The .txt files v1 exposes as line-lists (random.choice targets) vs. whole-text.
# Cookiebot_functions.txt is /commands help text — always used whole, never as lines.
_LINE_FILES = ("death", "sorte", "ship_dynamics", "answers")
_TEXT_ONLY_FILES = ("Cookiebot_functions",)
_ALL_TXT_FILES = _LINE_FILES + _TEXT_ONLY_FILES

# v1 stored a group's language as one of the literal strings "eng" | "pt" | "es"
# (Bot/Configurations.py:set_language), while Telegram's `language_code` and any
# future tenant config arrive as BCP-47-ish tags ('pt-BR', 'pt_br', 'en-US', ...).
# Normalise every shape seen in v1 or plausible from Telegram to the canonical
# 'en' | 'pt' | 'es'; anything else falls back to 'en', same as v1's default_lang.
_LANGUAGE_ALIASES: Mapping[str, str] = MappingProxyType(
    {
        "en": "en",
        "eng": "en",
        "english": "en",
        "en-us": "en",
        "en-gb": "en",
        "pt": "pt",
        "pt-br": "pt",
        "pt-pt": "pt",
        "por": "pt",
        "portuguese": "pt",
        "es": "es",
        "es-es": "es",
        "es-ar": "es",
        "es-mx": "es",
        "spanish": "es",
        "esp": "es",
    }
)


def resolve_language(code: str | None) -> str:
    """Map any v1/Telegram-shaped language code to the canonical 'en'/'pt'/'es'.

    Unknown or absent input resolves to 'en', matching v1's `default_lang`.
    """
    if not code:
        return _DEFAULT_LANGUAGE
    normalized = code.strip().lower().replace("_", "-")
    if normalized in _LANGUAGE_ALIASES:
        return _LANGUAGE_ALIASES[normalized]
    # Fall back to the primary BCP-47 subtag ('pt-XX' -> 'pt') before giving up,
    # so a locale variant we didn't enumerate still resolves sensibly.
    primary = normalized.split("-", 1)[0]
    return _LANGUAGE_ALIASES.get(primary, _DEFAULT_LANGUAGE)


def _read(lang: str, filename: str) -> str:
    package = f"{_DATA_PACKAGE}.{lang}"
    return resources.files(package).joinpath(filename).read_text(encoding="utf-8")


def _load_catalog(lang: str) -> Mapping[str, str]:
    """`lib.json` as v1 ships it, with `cb.json` layered on top.

    Two files, because `lib.json` is a byte-for-byte copy of v1's and
    `packages/cb-core/tests/test_locales.py` asserts exactly that — the whole
    point of copying it rather than retyping it. A v2-only string added to it
    breaks that guarantee (and did: `handler_error` landed there in f90e4a2 and
    took the byte-identity test with it, which is how it also went unnoticed
    that the string used `{trace}` where this module substitutes `%(trace)s`).

    `cb.json` is where a string this bot invented belongs. It overlays rather
    than merges under, so a v2 string can also deliberately override a v1 one
    without editing the copy.
    """
    catalog = dict(json.loads(_read(lang, "lib.json")))
    catalog.update(json.loads(_read(lang, "cb.json")))
    return MappingProxyType(catalog)


def _load_lines(text_body: str) -> tuple[str, ...]:
    # v1's Localizer.get_random_line strips and drops blank lines before choosing;
    # replicate that so lines() returns exactly what v1 would pick from.
    return tuple(ln.strip() for ln in text_body.splitlines() if ln.strip())


# ---- loaded once at import; no I/O after this point ----
_CATALOGS: Mapping[str, Mapping[str, str]] = MappingProxyType(
    {lang: _load_catalog(lang) for lang in LANGUAGES}
)
#: `lib.json` alone, per language — what `missing_keys()` reports on.
#: Kept separate because the two files answer different questions: drift in
#: here is v1's, inherited and outside our control, while a key missing from a
#: `cb.json` is usually a v2 decision (`publisher_ask_prompt` is deliberately
#: absent from `es` so an `es` group is prompted in English, exactly as v1 does
#: — `docs/contracts/util_postgetter.md`, D-PG-3). Reporting them together
#: turned an assertion about v1's data into one that changes every time this
#: bot adds a string.
_LIB_CATALOGS: Mapping[str, Mapping[str, str]] = MappingProxyType(
    {lang: MappingProxyType(dict(json.loads(_read(lang, "lib.json")))) for lang in LANGUAGES}
)
_TEXTS: Mapping[tuple[str, str], str] = MappingProxyType(
    {(lang, name): _read(lang, f"{name}.txt") for lang in LANGUAGES for name in _ALL_TXT_FILES}
)
_LINES: Mapping[tuple[str, str], tuple[str, ...]] = MappingProxyType(
    {(lang, name): _load_lines(_TEXTS[(lang, name)]) for lang in LANGUAGES for name in _LINE_FILES}
)


def get(key: str, lang: str = "en", **fmt: object) -> str:
    """Look up `key` in lib.json for `lang`, substituting %(name)s placeholders.

    Falls back en -> the key string itself; never raises on a missing key or
    language. Fallbacks are counted via the existing cache-lookup metric so
    catalog drift is observable without a new metric.
    """
    canonical = resolve_language(lang)
    value = _CATALOGS[canonical].get(key)
    if value is None and canonical != _DEFAULT_LANGUAGE:
        metrics.cache_lookups_total.labels(cache="locale", layer="language", outcome="miss").inc()
        value = _CATALOGS[_DEFAULT_LANGUAGE].get(key)
    if value is None:
        metrics.cache_lookups_total.labels(cache="locale", layer="key", outcome="miss").inc()
        return key
    if fmt:
        try:
            return value % fmt
        except (KeyError, ValueError, TypeError):
            # Malformed placeholder in the catalog data is a content bug, not a
            # reason to crash a reply — return the unformatted string, as v1 does.
            return value
    return value


def nested_value(section: str, key: str, lang: str = "en") -> object | None:
    """One entry out of a *nested* catalog object, falling back per entry.

    Several of v1's features keep their strings in an object rather than at the
    top level — `captcha`, `giveaway`, `destroy`, `battle_*`. `get()` resolves
    flat keys only, so those used to be reached by hand at each call site, and
    the hand-rolled versions fell back per *object*: if `es` had the object at
    all, a sub-key missing from it resolved to nothing. That is the common case
    rather than the rare one — v1's `es` catalog has a `giveaway` object with
    ten of its sixteen entries absent — so the fallback has to be per entry,
    which is what this does and what `get()` already does for flat keys.

    Returns the raw value (a `str` for most, a `list` for `giveaway.buttons`,
    a `dict` for `giveaway.winnner`), or `None` when neither language has it.
    """
    for candidate in (resolve_language(lang), _DEFAULT_LANGUAGE):
        section_value = _CATALOGS[candidate].get(section)
        if isinstance(section_value, dict) and key in section_value:
            return section_value[key]
    metrics.cache_lookups_total.labels(cache="locale", layer="nested", outcome="miss").inc()
    return None


def get_nested(section: str, key: str, lang: str = "en", **fmt: object) -> str:
    """`nested_value` as a formatted string, with `get()`'s exact conventions:
    missing returns `"<section>.<key>"`, malformed substitution returns the
    unformatted value rather than raising."""
    value = nested_value(section, key, lang)
    if not isinstance(value, str):
        return f"{section}.{key}"
    if fmt:
        try:
            return value % fmt
        except (KeyError, ValueError, TypeError):
            return value
    return value


def lines(name: str, lang: str = "en") -> tuple[str, ...]:
    """The non-empty lines of one of v1's line-list files (death, sorte, ...)."""
    if name not in _LINE_FILES:
        raise ValueError(f"{name!r} is not a line-list file; expected one of {_LINE_FILES}")
    canonical = resolve_language(lang)
    return _LINES[(canonical, name)]


def text(name: str, lang: str = "en") -> str:
    """The full contents of one of v1's .txt files, verbatim."""
    if name not in _ALL_TXT_FILES:
        raise ValueError(
            f"{name!r} is not a known locale text file; expected one of {_ALL_TXT_FILES}"
        )
    canonical = resolve_language(lang)
    return _TEXTS[(canonical, name)]


def catalog(lang: str) -> Mapping[str, str]:
    """The frozen lib.json mapping for `lang` (resolved to its canonical code)."""
    return _CATALOGS[resolve_language(lang)]


def missing_keys() -> dict[str, tuple[str, ...]]:
    """Per-language `lib.json` keys present in `en` but absent there.

    v1's own drift, inherited with the files (`docs/contracts/locales.md`) —
    reported so it is visible, not treated as an error. `cb.json` is
    deliberately excluded: see `_LIB_CATALOGS`. Use `missing_cb_keys()` for
    this bot's own catalogs.
    """
    en_keys = set(_LIB_CATALOGS[_DEFAULT_LANGUAGE])
    return {
        lang: tuple(sorted(en_keys - set(_LIB_CATALOGS[lang])))
        for lang in LANGUAGES
        if lang != _DEFAULT_LANGUAGE
    }


def missing_cb_keys() -> dict[str, tuple[str, ...]]:
    """The same, for `cb.json` — every entry is a v2 decision to justify.

    A key here means an `en` string this bot invented that a `pt`/`es` group
    will be answered in English for. Sometimes that is the port (D-PG-3);
    otherwise it is a gap. `packages/cb-core/tests/test_locales.py` enumerates
    the intended ones so an unintended one fails the build.
    """
    en_keys = set(json.loads(_read(_DEFAULT_LANGUAGE, "cb.json")))
    return {
        lang: tuple(sorted(en_keys - set(json.loads(_read(lang, "cb.json")))))
        for lang in LANGUAGES
        if lang != _DEFAULT_LANGUAGE
    }
