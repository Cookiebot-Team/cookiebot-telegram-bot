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
    return MappingProxyType(json.loads(_read(lang, "lib.json")))


def _load_lines(text_body: str) -> tuple[str, ...]:
    # v1's Localizer.get_random_line strips and drops blank lines before choosing;
    # replicate that so lines() returns exactly what v1 would pick from.
    return tuple(ln.strip() for ln in text_body.splitlines() if ln.strip())


# ---- loaded once at import; no I/O after this point ----
_CATALOGS: Mapping[str, Mapping[str, str]] = MappingProxyType(
    {lang: _load_catalog(lang) for lang in LANGUAGES}
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
    """Per-language keys present in `en` but absent there — drift, not an error."""
    en_keys = set(_CATALOGS[_DEFAULT_LANGUAGE])
    return {
        lang: tuple(sorted(en_keys - set(_CATALOGS[lang])))
        for lang in LANGUAGES
        if lang != _DEFAULT_LANGUAGE
    }
