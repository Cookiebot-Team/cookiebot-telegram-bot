# Contract: core_setlang (v1 -> v2)

Phase 2/6 of `/migrate-feature` for language selection. FEATURE-MAP row:
`core_setlang`, status "spec says web UI, bot does in-chat menu"
(`docs/site/content/docs/feature-map.mdx` §1, and §5 "`core_setlang` as a **web settings page** —
only in-chat `/configurar` exists"). This closes the locale loop opened by the
string-catalog port (`docs/contracts/locales.md`) and the `group_configs` port
(`docs/contracts/group-config.md`): both already exist and are used here
unmodified, not rebuilt.

v1 source read for this port:

- `../COOKIEBOT-Telegram-Group-Bot/Bot/Configurations.py:242-251` — `set_language`.
- `../COOKIEBOT-Telegram-Group-Bot/Bot/Configurations.py:79-101` —
  `set_language_commands` / `set_private_commands`.
- `../COOKIEBOT-Telegram-Group-Bot/Bot/COOKIEBOT.py:121-135` — the "bot itself
  was just added to a group" branch, `set_language`'s only call site.
- `../COOKIEBOT-Telegram-Group-Bot/Bot/universal_funcs.py:280-289` —
  `set_bot_commands`, the actual `setMyCommands` HTTP call.
- `../COOKIEBOT-Telegram-Group-Bot/Bot/Configurations.py:139-240` —
  `configurar`/`config_variable_button`/`configurar_set`, already ported as
  `handlers/config_menu.py` by another agent (out of scope here, read only to
  find the boundary).

## What v1 has outside the `/config` menu

Read literally: **the "Language" menu button (callback letter `k`) is v1's only
user-facing language *picker*.** Everything else v1 does is not a second picker —
it is plumbing around that one picker plus one automatic first-contact default:

1. **First-contact derivation** (`set_language`, `Configurations.py:242-251`,
   called only from `COOKIEBOT.py:133-134`): when the bot is added to a new
   group, and the adder's Telegram update carries a `language_code`, v1
   synthesizes a fake "reply to the language prompt" and funnels it through the
   *same* write path `/config`'s Language button uses (`configurar_set`), so a
   brand-new group's language is pre-set from the adder's own Telegram client
   instead of staying on the hardcoded default (`"pt"`, `Configurations.py:111`).
   This is genuinely missing in v2 — nothing derives a language at first
   contact; a new group always gets `settings.default_language`.
2. **`setMyCommands` relabeling** (`set_language_commands`,
   `Configurations.py:79-98`): a side effect v1 bundles into *every* successful
   language write, called from both `set_private_commands` (v1's `/start`
   command-menu, not group-scoped) and `configurar_set`'s language branch (the
   `/config` menu's actual write). `handlers/config_menu.py`'s own docstring and
   `docs/contracts/util_config.md` design decision #5 already recorded that this
   side effect is **not** reproduced there — "a `core_setlang`/command-menu
   concern, out of scope for the files this port owns." That gap is this port's
   second deliverable.

So: **nothing v1 does outside the `/config` menu is a second command or a
second picker.** There is no `/setlang`, no `/language`, no alias to add to
`cb_core/textmatch.py`. The two things genuinely missing are the ones the task
brief already named, and this contract does not invent a third.

## Phase 2 — behaviour contract

| Aspect | v1 behaviour (file:line) |
|---|---|
| Trigger (derivation) | The bot itself being added to a group (`content_type == "new_chat_member"` and `msg['new_chat_participant']['id'] == myself['id']`, `COOKIEBOT.py:122`), **and** `'language_code' in msg['from']` (`:133`) — the adder's Telegram client language, not the group's. If the key is absent, `set_language` is never called and the group keeps whatever default it would otherwise get. |
| Derivation mapping | `Configurations.py:243-248`: `'pt' in language_code -> "pt"`; `elif 'es' in language_code -> "es"`; `else -> "eng"`. Case-sensitive, substring (not prefix/equality) match, no normalisation. See the mapping table below. |
| Trigger (relabeling) | Any successful write to the `language` field via `configurar_set` (`Configurations.py:176-177`), and `set_private_commands` (`/start` in a private chat — out of scope, see above). |
| Scope | Always `BotCommandScopeChat` (`universal_funcs.py:280-289`), never the default or all-private-chats scope. |
| Command source | `i18n.get_file("Cookiebot_functions.txt", lang=<target language>)`'s "MANUAL COMMANDS" block, parsed by an inline filter keeping only single-word, all-lowercase `command - description` lines (`Configurations.py:82-87`). |
| Languages relabeled | All three v1 UI languages (`pt`, `es`, `eng` -> Telegram `language_code` `pt`/`es`/`en` via `language[0:2].lower()`, `universal_funcs.py:283`), **all carrying the same target-language command list** — so the chat's command menu reads in the group's chosen language no matter which of the three the viewer's own Telegram client is set to (`Configurations.py:91-94`). |
| Confirmation message | Sent to a `chat_id` v1 hardcodes per call site (the bot owner's DM for the join-time path, since `set_language` calls `configurar_set(cookiebot, msg, ownerID)` — `Configurations.py:251`; the admin's own DM for the `/config` path). Literal text, chosen by the *target* language only, three branches (`Configurations.py:97`, quoted verbatim in `set_group_commands._confirmation_text`). |
| Failure handling | `set_bot_commands` (`universal_funcs.py:280-289`) is wrapped in `@retry_with_backoff`, which only re-raises genuine transient network errors; a real Telegram-side rejection (4xx) is never inspected — `r.text` is returned and nothing downstream checks it for success. In practice v1 **never** fails the language change itself over a rejected `setMyCommands` call, because nothing in the call chain would notice. |
| Known defect not reproduced | `i18n.get_file` returns the whole file as one `str` (confirmed by reading `Bot/loc.py:127-131`), so v1 HEAD's `for line in lines:` (`Configurations.py:81`) iterates **characters**, not lines — `comandos` is always empty on every real invocation today. Confirmed against v1's own git history: a prior `.readlines()`-based version was refactored to `i18n.get_file` without adding `.splitlines()`. This is a silent-failure bug (AGENTS.md Phase 2: "silent-failure bugs get fixed"), not a user-visible quirk — fixed here, not reproduced. |
| Persistence | `group_configs.language`, the same column and the same write path (`group_config.set_config`) `/config`'s Language button already uses — no new table, no new column. |

## The language-derivation mapping table

| Input `language_code` | `derive_join_language` output | Why |
|---|---|---|
| `"pt-BR"` | `"pt"` | contains `"pt"` |
| `"pt-br"` | `"pt"` | contains `"pt"` (already lowercase) |
| `"pt"` | `"pt"` | contains `"pt"` |
| `"es-419"` | `"es"` | contains `"es"` |
| `"es-AR"` | `"es"` | contains `"es"` |
| `"en-GB"` | `"eng"` | contains neither `"pt"` nor `"es"` -> `else` |
| `"en"` | `"eng"` | same |
| `"de"`, any other real BCP-47 tag not containing `pt`/`es` | `"eng"` | `else` |
| `""` (present but empty) | `"eng"` | v1 gates on the **key**, not the value; an empty string still reaches the `else` branch |
| absent / `None` | `None` (no write at all) | v1 gates on `'language_code' in msg['from']`; the key being absent means `set_language` is never called |
| `"PT-BR"` / `"ES"` (uppercase) | `"eng"` | v1 never lowercases `language_code`; the substring check is case-sensitive, so an uppercase tag falls to `else` |
| `"chapter"` (garbage that happens to contain `"pt"`) | `"pt"` | preserved quirk: a naive substring match, not a prefix/equality match — anything containing the two letters `p`,`t` consecutively matches, real locale tag or not |

The stored output is v1's own literal `"pt"`/`"es"`/`"eng"` strings, not
`cb_core.locales`'s canonical `"en"`/`"pt"`/`"es"` — matching what
`group_configs.language` already stores for a value written through `/config`
(`qa/integration/test_config_menu.py:test_language_field_press_writes_v1s_literal_string`).
`set_group_commands` resolves whichever literal or canonical form it is handed
through `cb_core.locales.resolve_language` before choosing a catalog, so both
forms work as input to it.

## `setMyCommands` contents and failure policy

- **Commands**: the "MANUAL COMMANDS" block of the target language's
  `Cookiebot_functions.txt` (`cb_core.locales.text("Cookiebot_functions", lang)`),
  filtered to single-word, all-lowercase `command - description` rows — exactly
  v1's filter, minus the character-iteration bug (see above). For English this
  is 37 commands (`everyone`, `adm`, `anything`, ... `privacy`); `pt`/`es` carry
  the same count under their own localised command spellings (`qualquercoisa`
  vs `cualquiercosa`, etc.) — verbatim data already ported by the string-catalog
  work, read here, not modified.
- **Languages**: all three of `pt`, `es`, `en` (Telegram `language_code` scope
  values), every one of them carrying the *same* target-language command list —
  not three different localisations of the command list itself.
- **Scope**: `BotCommandScopeChat(chat_id=<the group>)` — never the bot-wide
  default scope, so this never touches any other chat's command menu.
- **Failure policy** (a decision v1 never actually made — see the contract
  table above): a rejected `setMyCommands` call for any one of the three scopes
  is logged (`structlog`, `error=str(exc)`) and does **not** raise, and does
  **not** roll back or block the `group_configs.language` write that already
  landed before this runs. `set_group_commands` still attempts all three scopes
  even if an earlier one failed, and returns `False` if any of them did, purely
  informationally. Rationale: the language change itself (the write) is the
  behaviour a user can observe and rely on; the command-menu relabeling is
  cosmetic, and v1's own implementation was already incapable of noticing a
  rejection (see the "failure handling" row above), so a stricter v2 policy
  would be inventing a failure mode v1 never had, not fixing one.

## What genuinely does not exist and was not invented

- No new command, no new alias — nothing to add to `cb_core/textmatch.py`.
- No new table, no new `group_configs` column.
- No admin-facing UI beyond what `/config`'s Language button (owned elsewhere)
  already provides.

## Boundary: what is deliberately not reproduced

`COOKIEBOT.py:121-135` bundles four unrelated things into "the bot itself was
just added to a group":

1. A blacklist/short-title/bot-name-in-title auto-leave gate.
2. A celebratory `sendAnimation` (skipped for alternate-bot skins).
3. An owner DM (`"Added\n{chatinfo}"`).
4. The language derivation (this port).

Only (4) is built in `handlers/setlang.py`. Consequence, recorded plainly: v1
only reaches `set_language` after (1) does *not* trigger an auto-leave; v2's
`on_bot_added_to_group` has no such gate (no onboarding/blacklist feature is
wired here), so today it always derives a language on join, including for a
group that some future onboarding feature would auto-leave. That feature, once
it exists, needs to run its gate before (or instead of) this handler. Also not
reproduced: (2) and (3), and the confirmation message's target chat (v1
hardcodes the bot owner's DM for the join-time path via `ownerID`, an
accidental consequence of code reuse rather than a deliberate design) —
`set_group_commands`/`apply_join_language` expose `notify_chat_id` so a future
caller can choose a target, but this port does not default one, since there is
no existing "owner DM" convention in v2 to plug into and inventing one is out
of scope for a two-item task brief.

## Needed in files this port does not own

- **`handlers/__init__.py:build_router()`** — `setlang.router` is not included.
  Needs `root.include_router(setlang.router)`, and it must run *before*
  `welcome.router`: `welcome.py`'s `on_join` also matches `F.new_chat_members`
  unconditionally and returns (without `raise SkipHandler()`) when the joiner is
  the bot itself, so if `welcome.router` runs first, aiogram treats the update
  as already handled and `setlang`'s handler never fires. This is the same
  class of ordering gap `docs/contracts/core_welcome.md`'s "Boundary" section
  already flags for `core_groupguardian`/`util_doomlist`.
- **`handlers/config_menu.py`** — design decision #5 in `docs/contracts/util_config.md`
  already records that a language write through the `/config` menu does not call
  `set_group_commands`. Wiring `apply_config_reply`'s language branch to call
  `cb_gateway.handlers.setlang.set_group_commands(bot, group_id, value)` after a
  successful `apply_change` would close that gap; not done here since
  `config_menu.py` is out of scope for this task.
- No change needed to `cb_core/locales.py` or `cb_core/group_config.py` — both
  are used exactly as documented in their own contracts.

## Phase 2/6 — QA vs. v1 conflict

`../Cookiebot-QA/features/core_setlang.feature` describes a **web settings
page**: a "Cookiebot settings page" with a "language settings page" the user
navigates to and picks a language from. No such surface exists in v1 (a Python
`telepot` bot with no web frontend) or in v2's gateway/API today.
`docs/site/content/docs/feature-map.mdx` already records this exact mismatch (§1 row `core_setlang`,
§5 "core_setlang as a web settings page — only in-chat /configurar exists").
Per AGENTS.md §1 ("v1 code wins for observable behaviour... record the conflict
rather than silently picking"): the three copied scenarios are kept verbatim in
`qa/features/core_setlang.feature` (not reworded — per `/migrate-feature`'s
Phase 3, "keep the wording of existing scenarios; only add"), and their steps in
`qa/test_core_setlang.py` drive the closest real trigger this port owns — a new
group's language being derived from the adder's own Telegram client language —
treating the QA scenario's "user selects a language" as *intent* ("the bot ends
up responding in language X") rather than literal wording that maps to no real
code path. Five further scenarios were added, covering the actual mechanism
directly (pt/es/en derivation, missing `language_code`, and a rejected
`setMyCommands` call not undoing the write).

## Phase 6 — parity table

| Aspect | Verdict |
|---|---|
| First-contact derivation exists at all | **changed (fixed — was entirely missing)**: v2 had no equivalent before this port; a new group always got the tenant default. |
| Derivation mapping (`pt`/`es`/`eng`, substring match, case sensitivity, garbage handling) | same — `derive_join_language` reproduces v1's exact `if/elif/else` chain, including the case-sensitivity and substring-not-prefix quirks. |
| Stored value is v1's literal string, not an ISO code | same — matches `/config`'s own write path (`docs/contracts/group-config.md`). |
| Missing `language_code` leaves the group untouched | same — `None` in, `None` out, no write attempted. |
| `setMyCommands` scope | same — `BotCommandScopeChat`, never the default scope. |
| `setMyCommands` command source | same — the target language's "MANUAL COMMANDS" block. |
| `setMyCommands` languages covered | same — `pt`/`es`/`en`, all carrying the target language's command list. |
| Command list is non-empty | **changed (bug — fixed)**: v1 HEAD sends an empty list on every real invocation (`for line in lines:` over a whole-file `str`); this port actually splits into lines first. |
| `setMyCommands` failure policy | **changed (decided, not previously decided)**: v1 never noticed a rejection either; this port makes that explicit — logged, non-fatal, does not undo the `group_configs` write. |
| Confirmation message target (owner DM for the join path) | **not reproduced** — no existing "owner DM" convention in v2 to plug into; `notify_chat_id` is exposed but not defaulted. Flagged above, not fixed silently. |
| Blacklist/short-title auto-leave gate before deriving | **not reproduced** — a different feature's responsibility, not yet wired anywhere in v2. Flagged above. |
| Celebratory join animation, owner "Added" notice | **not reproduced** — not this feature's job (FEATURE-MAP lists them under other rows). |
| `/config` menu's language write triggers `setMyCommands` | **not built here** — `config_menu.py` out of scope; the function it needs (`set_group_commands`) now exists for its owner to call. |
| New command or alias | **none added** — v1 has no second picker outside `/config`; nothing invented. |

## Testing

- **Unit** (`packages/cb-gateway/tests/test_setlang.py`): `derive_join_language`
  over the full mapping table above (including the case-sensitivity and
  substring-quirk cases); `parse_manual_commands` against the real, ported
  locale text for all three languages, proving the character-iteration bug is
  not reproduced; `set_group_commands` against a hand-rolled fake bot (not
  aiogram's real client, not our own code) proving the three-scope relabeling,
  the v1-literal-vs-canonical-language equivalence, and the non-raising failure
  policy (a rejected scope does not raise, the other scopes are still
  attempted, no confirmation is sent on failure); `apply_join_language`'s
  composition (no-op without a `language_code`, writes+relabels with one).
- **Acceptance** (`qa/features/core_setlang.feature` + `qa/test_core_setlang.py`):
  drives `setlang.router` (via a local `Dispatcher` override, since it is not
  wired into `cb_gateway.main.dp` yet) against the mock Telegram API and a real
  database, covering the three copied QA scenarios (reinterpreted per the
  conflict above) plus pt/es/en derivation, no-`language_code`, and a rejected
  `setMyCommands` call not undoing the language write.
- **Integration** (`qa/integration/test_setlang.py`, `@pytest.mark.integration`):
  `apply_join_language` against a real Citus database, proving the language
  change actually lands in `group_configs` for a brand-new group with no prior
  row, using a lightweight fake bot double for the Telegram side (the outside
  world, not our own code — AGENTS.md §6 allows mocking that).
