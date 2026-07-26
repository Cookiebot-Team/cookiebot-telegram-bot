# Contract: locale string catalog (v1 -> v2)

Phase 2 of `/migrate-feature` for the string catalog. This is a **data port**: the
owner has already decided the flat files stay the source of truth (a Postgres
override layer for tenant branding comes later, on top, not instead). There is no
new observable behaviour to design — the contract is "byte for byte", so this
table records provenance and the drift that already exists in v1, not a design
decision.

| Aspect | v1 behaviour (with file:line) |
|---|---|
| Source of truth | `../COOKIEBOT-Telegram-Group-Bot/Bot/Static/locales/{eng,pt,es}/`: `Cookiebot_functions.txt`, `answers.txt`, `death.txt`, `ship_dynamics.txt`, `sorte.txt`, `lib.json`. Read at request time by `Bot/loc.py:Localizer`, `lru_cache`'d per language. |
| Directory naming | v1 uses `eng` for English; v2 canonicalises to `en`. `pt` and `es` are unchanged. |
| Language storage | A group's language is stored as the literal string `"eng"`, `"pt"` or `"es"` (`Bot/Configurations.py:242-251 set_language`), set from the *first* message's `msg['from']['language_code']` (`Bot/COOKIEBOT.py:133-134`), then user-overridable via the config menu. |
| Lookup semantics | `Localizer.get(key, lang=...)` walks `"a.b.c"` dotted paths, substitutes `%(name)s` via `%`-formatting, and merges a fallback chain ending in `default_lang="eng"` (`Bot/loc.py:66-100`). v1's configured fallback chain also chains `pt`/`es` through `eng`, i.e. every language ultimately falls back to English. |
| Line-list files | `death.txt`, `sorte.txt`, `ship_dynamics.txt`, `answers.txt` are read whole then split into non-empty, stripped lines; `Localizer.get_random_line` (`Bot/loc.py:131-138`) `random.choice`s one. `Cookiebot_functions.txt` (the `/commands` help text) is always used whole, never as a line list. |
| Missing key | `Localizer.get` returns `default` (`None` unless the caller passes one) rather than raising — v1 has no metric or log for this today. |
| Known catalog drift | `lib.json` key sets are **not** identical across languages (this is pre-existing v1 data, not introduced by the port): `pt` is missing `caption`, `groups`, `private_chat`, `teste` (present in `en`) and has two keys `en` lacks (`battle_title_list`, `battle_title_plus`); `es` is missing `age`, `caption`, `complaint`, `dice_exemple`, `dice_roll`, `groups`, `private_chat`, `teste`. Preserved as-is and asserted explicitly in `test_locales.py` so the drift stays visible instead of being silently "fixed" by a rewrite. |

## v2 design (mechanical translation of the above, not new behaviour)

- Files are copied verbatim (`cp`, no reformatting) into
  `packages/cb-core/src/cb_core/locale_data/{en,pt,es}/`, same filenames.
- `cb_core/locales.py` loads every file once at import via `importlib.resources`
  into `MappingProxyType`/tuple structures — no I/O, no cache invalidation, no
  global mutable state on the request path (this is exactly what replaces v1's
  un-invalidated per-process dict cache, FEATURE-MAP D6).
- `resolve_language(code)` normalises any v1-stored code (`"eng"`) or
  Telegram/BCP-47-shaped code (`"pt-BR"`, `"pt_br"`, `"es-AR"`, ...) to the
  canonical `"en" | "pt" | "es"`; unknown or `None` resolves to `"en"`, matching
  v1's `default_lang`.
- `get()` replicates v1's fallback-to-English and `%`-formatting, but never
  returns `None` on a miss (v1's contract for that case is undefined behaviour
  downstream, since every real caller passed a `default`): falls back further to
  the raw key string, and counts both the language-level and key-level fallback
  via `cb_core.metrics.cache_lookups_total(cache="locale", layer=..., outcome="miss")`
  so drift becomes observable instead of silently swallowed as it is in v1.
- `missing_keys()` exposes the drift above programmatically instead of the
  operator having to notice a blank string in a live chat.

## Who calls this

A handler resolves the group's language once (from stored config, defaulting per
`resolve_language(None) == "en"`) and passes it into `get()`/`lines()`/`text()`.
This module does not read group config itself — that stays with whatever owns
group settings in `cb-api`/`cb-gateway`; wiring that call site is out of scope
for this port and belongs to whichever handler consumes each string.
