# Contract: util_config (v1 -> v2)

Phase 2/6 of `/migrate-feature` for the in-chat admin configuration menu. v1's
entire flow lives in `../COOKIEBOT-Telegram-Group-Bot/Bot/Configurations.py`:
`configurar` (`:139-167`, the `/configurar`/`/configure` entry point),
`config_variable_button` (`:213-240`, a menu button press) and `configurar_set`
(`:169-211`, applying the admin's reply). The dispatcher wiring is
`COOKIEBOT.py:82` (private-chat reply routing), `:278-280` (`/configurar`/
`/configure` trigger) and `:360-361` (the `CONFIG` callback branch).
`cb_core.textmatch.COMMAND_ALIASES` already maps `configurar`/`configure`/
`config` to the canonical `config` command — both v1's trigger and QA's spelling
(`Cookiebot-QA/features/util_config.feature`, which spells it `/config`) resolve
with no change needed there.

v2 lives entirely in `packages/cb-gateway/src/cb_gateway/handlers/config_menu.py`.

## Phase 2 — behaviour contract

| Aspect | v1 behaviour (file:line) |
|---|---|
| Triggers | `/configurar`, `/configure` (`COOKIEBOT.py:278`); QA additionally expects `/config` (mismatch, both now resolve via `textmatch.COMMAND_ALIASES`). Only fires in a group/supergroup chat — the private-chat branch of `thread_function` has no `/configurar` case at all. |
| Preconditions | Admin-gated. Check is `'creator' in listaadmins_status and str(from_id) not in listaadmins_id and str(from_id) != str(ownerID)` (`Configurations.py:141`) — the `'creator' in ...` clause is dead weight (true for every real group), so this reduces to "not in the id list and not the bot owner". No feature-flag gate (unlike `functionsFun`/`functionsUtility`). |
| Anonymous-admin defect | An anonymous admin's `from.id` is Telegram's synthetic `GroupAnonymousBot`, never in `listaadmins_id`, so this check **always rejects a genuine admin posting anonymously** — shown the permission-denied text plus `Static/remove_anonymous_tutorial.mp4` telling them to turn off a Telegram feature that was never the problem (`Configurations.py:141-144`). Full analysis: `docs/contracts/admins.md`. |
| Success output | Sends the requester's own private chat (`msg['from']['id']`) a message: `"Current settings:\n\n" + variables + "\n\nChoose the variable you would like to change\n\n(If you want to change rules or welcome message, use /newrules or /newwelcome on the group)"` plus a 13-button, one-per-row inline keyboard (`:149-164`). Then replies in the group: `"Te mandei uma mensagem no chat privado para configurar!"` (machine-translated for non-pt groups via `translate()`, `universal_funcs.py:139-163`, a live Google Cloud Translate call). |
| Failure output (DM blocked) | `try`/`except` around the DM `sendMessage` call (`:148-167`): on failure, replies in the group `"Não consegui te mandar o menu de configuração\n<blockquote> Mande uma mensagem no meu chat privado para que eu consiga fazer isso) </blockquote>"` (same machine-translation path). |
| Failure output (not admin) | Replies in the group: `"Você não tem permissão para configurar o bot, ou está anônimo!\n<blockquote> Você está falando como usuário e não como canal? A permissão 'permanecer anônimo' deve estar desligada! </blockquote>"` (machine-translated) **and** uploads/sends `Static/remove_anonymous_tutorial.mp4` (`:143-144`) — unconditionally, even though the message's own wording is only sometimes about anonymity. |
| Menu buttons | 13 buttons, one per row, in this exact order: Language(k), FurBots(a), Stickers limit(b), 🕒 Limbo(c), 🕒 CAPTCHA(d), Fun Functions(h), Utility Functions(i), SFW Chat(j), Publisher Post(m), Publisher Ask(n), Thread Posts(o), Max Posts(p), Publisher Members Only(q). Labels and prompt text are hardcoded English, unlocalized, regardless of group language (`:150-163`, `:215-240`). |
| Callback data | `"{letter} CONFIG {chat_id}"`, e.g. `"a CONFIG -1001234567890"` (`:150-163`). |
| Callback handling | `config_variable_button` (`:213-240`) sends a prompt in the same chat (the admin's DM): `f"Chat = {chat}\n{field prompt}\n\nREPLY THIS MESSAGE with the new variable value"`. **Never calls `answerCallbackQuery`** (`COOKIEBOT.py:360-361` dispatches straight to the handler with no answer) — a real Telegram client's loading spinner never stops. No permission recheck at this step (relies entirely on the DM having only ever reached the authorized admin). |
| Write path | `configurar_set` (`:169-211`), triggered by any private-chat reply whose `reply_to_message.text` contains `"REPLY THIS MESSAGE with the new variable value"` (`COOKIEBOT.py:82`). Extracts the target `chat_to_alter` from the prompt's first line (`"Chat = {id}"`), matches the field by an if/elif chain of substring checks against the same prompt text, coerces `msg['text'].lower()` per field (`bool(int(x))`, `int(x)`, or the raw string for language/thread id), and `PUT`s the whole 13-field config back. On success: `react_to_message(msg, '👍')`, then `"Successfully changed the variable!\nSend /reload in the chat if the old config persists"`. On an empty reply: `"ERROR: invalid input\nTry again"`. |
| Un-validated input | `if new_val or new_val in ["pt", "eng", "es"]:` is a no-op condition (true for any non-empty string) — v1 validates nothing beyond non-empty. A non-numeric reply to a numeric field (`b`,`c`,`d`,`o`,`p`) makes `int(new_val)` raise, **uncaught**, propagating to `thread_function`'s top-level `except Exception` — a traceback mailed to the bot owner, total silence to the user. |
| Side effects | Writing the `language` field (`k`) also calls `set_language_commands` (`:79-98`): relabels the group's Telegram command menu (`setMyCommands`) in three languages and sends a further confirmation to the admin's DM. This is a `core_setlang`/command-menu concern (FEATURE-MAP), not a `group_configs` write. |
| Persistence | `PUT configs/{chat_to_alter}` (Java backend, 13 fields) plus the process-local `cache_configurations[chat_id] = current_configs` (`Configurations.py:9`, `:207`) — one of five independent per-process caches, FEATURE-MAP D6; a change only reaches other replicas via a manual `/reload` typed into each of them separately. |
| Known defects carried | D6 (unbounded per-process cache); the anonymous-admin bug (`docs/contracts/admins.md`); the uncaught-parse-exception silent failure above; the missing `answerCallbackQuery`. |

## Design decisions for v2

All of these are also called out inline in `config_menu.py`'s module docstring.

1. **Anonymous-admin bug: fixed, not reproduced.** `ctx.is_admin` (via
   `cb_core.admins.resolve_actor`) already treats an anonymous sender as an
   admin. But an anonymous admin has no real Telegram user id to open a DM
   with (`ctx.actor.user_id is None`), so v2 routes them into the same
   "couldn't reach you privately" branch v1 already has for a *known* admin
   whose DM fails — the closest existing v1 behaviour to "you're an admin, but
   I can't message you," rather than inventing a new message. This is the
   scenario the task specifically asked to cover: "Admin using anonymous mode
   uses /config command" in `qa/features/util_config.feature`.
2. **Every callback is answered.** Both `press_config_button` and the implicit
   fallthrough inside it call `callback.answer()` unconditionally — the fix for
   v1's permanent spinner.
3. **No `/reload` instruction.** `group_config.set_config` already invalidates
   every replica (L1 drop here, L2 delete, pub/sub publish) — the success
   message no longer tells the admin to type a command that would now be
   redundant advice.
4. **Bad input no longer crashes.** `parse_reply_value` catches the coercion
   failure and returns `None`, which the handler turns into v1's own
   `"ERROR: invalid input\nTry again"` (previously only shown for an empty
   reply) instead of letting an exception propagate to a silent failure.
5. **`setMyCommands` side effect not reproduced.** Changing `language` through
   this menu updates `group_configs.language` only. Relabelling the bot's
   command menu per chat is a separate Telegram API surface
   (`core_setlang`/command-menu), out of scope for the files this port owns.
6. **No locale-catalog entries exist for this menu.** Checked: no
   `config`-prefixed key in any `cb_core/locale_data/*/lib.json`. v1's own menu
   text is hardcoded English regardless of group language (only the three
   group-facing strings are live-translated via an external Google Translate
   call, `universal_funcs.py:139-163`, which this port does not reproduce).
   Since `cb_core/*` is out of scope for this port, the three group-facing
   strings are hand-translated literals living in `config_menu.py`
   (`_DENIED_TEXT`/`_SENT_DM_TEXT`/`_CANNOT_DM_TEXT`), not a `locales.get()`
   lookup — **reported as a gap for whoever owns the locale catalog**: if
   `util_config` (or any future menu-shaped feature) is meant to route through
   `cb_core.locales`, it needs these keys added there first.
7. **No re-authorization at write time, matching v1.** Neither the callback
   press nor the reply-apply step rechecks that the replying user is still an
   admin of the target group — same trust model as v1 (the DM thread itself is
   the only access control, since only the authorized admin who opened
   `/config` ever receives it). Not hardened further: v1 has no bug here to
   fix, and adding new authorization logic with no QA coverage risks its own
   defects for no v1-observed problem.
8. **No menu timeout, matching v1.** Unlike `RULES`/`ADM` callbacks
   (`COOKIEBOT.py`'s 600-second staleness checks), v1's `CONFIG` callback and
   the reply-apply path have no time limit at all — an old menu or a stale
   prompt still works indefinitely. Reproduced as-is (no expiry added); there
   is no "someone else's menu" case to guard against either, since only the
   requesting admin's own DM ever contains the menu.
9. **v2-only `group_configs` columns are not exposed in this menu.**
   `sticker_spam_window_s` and `doomlist_enabled` (see
   `docs/contracts/group-config.md`) have no v1 field and no v1 button — this
   port reproduces v1's exact 13-button menu, not a superset. They remain
   settable only through `group_config.set_config` directly (e.g. a future
   `/doomlist` toggle), not through `/config`.
10. **`_menu_text`'s summary omits `publisher_members_only`, matching v1.** v1's
    `variables` string stops at `configs[11]` (Max Posts) and never prints
    `configs[12]`, even though button `q` (Publisher Members Only) exists and
    works. Reproduced exactly — a cosmetic summary gap, not a functional one.
11. **No v2-hosted asset for the anonymous-mode tutorial video.**
    `Static/remove_anonymous_tutorial.mp4` is a literal file on v1's own
    filesystem; there is no equivalent in `cb_core.storage` yet.
    `config_menu._ANONYMOUS_TUTORIAL_VIDEO` is a placeholder CDN URL —
    **reported as a gap** for whoever owns the media/storage pipeline to
    supply a real asset reference.

## Testing note: why the write path only has an integration test

`group_config.get_config` degrades to `DEFAULTS` with no database reachable
(caught and logged), but `group_config.set_config` has **no equivalent
try/except around its `db.execute` call** — a database outage makes a write
raise instead of degrading. This asymmetry is `cb_core/group_config.py`, out of
scope for this port to change (reported to that module's owner as a finding,
not fixed here). Consequence: `qa/test_util_config.py` (mock Telegram, no
database) only exercises what stays read-only — opening the menu and pressing a
button to get a prompt. The actual row-changing write is covered by
`qa/integration/test_config_menu.py` against a real Citus instance, and by unit
tests of `parse_reply_value`/`_apply_change`'s pure inputs in
`packages/cb-gateway/tests/test_config_menu.py`.

Also note: `handlers/__init__.py:build_router()` is out of scope for this port
(other features are being ported in parallel against the same file) — this
router is not yet wired into `cb_gateway.main.dp`. `qa/test_util_config.py`
builds its own local `Dispatcher` around `config_menu.router` instead of
importing `cb_gateway.main.dp`; whoever integrates all M1 routers needs to add
`root.include_router(config_menu.router)` to `build_router()`.

## Phase 6 — parity table

| Aspect | Verdict |
|---|---|
| Triggers (`/configurar`, `/configure`, `/config`) | same — `textmatch.COMMAND_ALIASES` already covers all three |
| Group-only (no private-chat trigger) | same — `F.chat.type.in_({"group","supergroup"})` |
| Admin-only gate | same in effect — `ctx.is_admin` replaces the dead `'creator' in ...` membership check with an equivalent real check |
| Anonymous admin | **changed (bug — fixed)**: v1 always rejects with the tutorial video; v2 lets them pass the permission check and, since Telegram gives no real id to DM, tells them (in-group) it couldn't reach them privately instead |
| Menu: buttons, labels, order, callback format | same — `CONFIG_FIELDS` reproduces all 13 in v1's exact order, `build_callback_data` reproduces `"{letter} CONFIG {chat_id}"` |
| Menu: prompts per field | same — verbatim text, `build_prompt` |
| Menu: "Current settings" summary (omits Publisher Members Only) | same, including the omission |
| Write coercion (bool/int/topic/language) | same types and same lack of range validation, except: |
| Write: non-numeric input | **changed (bug — fixed)**: v1 crashes silently (uncaught exception, owner-only traceback email); v2 replies with v1's own `"ERROR: invalid input\nTry again"` |
| Write: success message | **changed (intentional)**: drops the now-false `"Send /reload..."` instruction, since `set_config` auto-invalidates every replica |
| Callback answered | **changed (bug — fixed)**: v1 never answers `CONFIG` callbacks (permanent spinner); v2 always does |
| `language` write side effect (`setMyCommands`) | **changed (deferred, out of scope)**: not reproduced — a command-menu concern, not a `group_configs` write |
| Persistence caching / `/reload` | same net effect as the rest of `group_config` (see `docs/contracts/group-config.md`) — no per-feature change needed here |
| Locale catalog use for menu strings | **changed (gap, reported)**: no keys exist yet in `cb_core.locales` for this menu; v1's own menu text is unlocalized anyway, but the three group-facing strings are hardcoded literals here rather than `translate()`'s live output |
| Menu/prompt timeout | same — neither version expires a stale menu or prompt |
| Write-time re-authorization | same — neither version rechecks admin status between button press and reply |
| Anonymous tutorial video asset | **changed (gap, reported)**: placeholder URL, no real v2 media asset exists yet |
