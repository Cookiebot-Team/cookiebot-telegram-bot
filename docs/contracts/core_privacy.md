# Contract: core_privacy (v1 -> v2)

Phase 2/6 of `/migrate-feature` for `/privacy`. QA:
`../Cookiebot-QA/features/core_privacy.feature`. FEATURE-MAP row: `core_privacy`,
status `✅`.

## Phase 2 — v1 behaviour contract

v1 handler: `privacy_statement`, `../COOKIEBOT-Telegram-Group-Bot/Bot/Miscellaneous.py:60-63`.

```python
def privacy_statement(cookiebot, msg, chat_id, language):
    send_chat_action(cookiebot, chat_id, "typing")
    text = i18n.get("privacy", lang=language)
    send_message(cookiebot, chat_id, text, msg_to_reply=msg, parse_mode="HTML")
```

Dispatched from two places in the `if/elif` chain in
`../COOKIEBOT-Telegram-Group-Bot/Bot/COOKIEBOT.py`:

| Aspect | v1 behaviour (file:line) |
|---|---|
| Triggers | `msg['text'].startswith(("/privacy", "/privacidade", "/privacidad"))` — prefix match, so also fires on `/privacy@bot`, `/privacy anything`, and technically `/privacyland`. Group branch: `COOKIEBOT.py:195-196`. Private-chat branch: `COOKIEBOT.py:87-88`, hardcoded `language='eng'` regardless of the sender's own language. |
| Preconditions | None. No admin check. Not gated by `functionsFun`/`functionsUtility` — the elif arm sits before any feature-flag check, so `/privacy` always answers even with both feature areas off. |
| Cooldowns / quotas | None. |
| Success output | `i18n.get("privacy", lang=language)` sent as a reply to the triggering message (`msg_to_reply=msg`), `parse_mode='HTML'`. Text (en): `"Cookiebot's privacy terms (and its clones) are available at https://cookiebotfur.net/privacy"`. Localised for `pt`/`es`, byte-identical values now live in `cb_core/locale_data/{en,pt,es}/lib.json` under key `"privacy"`. |
| Failure output | None — there is no failure branch; the lookup always resolves (v1's `i18n.get` falls back to `en` on a missing key/lang, same as `cb_core.locales.get`). |
| Persistence | None. |
| Side effects | `send_chat_action(cookiebot, chat_id, 'typing')` before the reply (cosmetic "typing…" indicator). |
| External calls | None. |
| Known defects | None specific to this handler. Inherits none of FEATURE-MAP's D1-D13 (no backend call, no shared cache, no file I/O). |

## Phase 3 — QA scenario

Copied `../Cookiebot-QA/features/core_privacy.feature` verbatim into
`qa/features/core_privacy.feature`, then added scenarios v1's code supports that
the spec didn't cover: the Portuguese/Spanish aliases, the `@botname` form, and a
command addressed at a different bot being ignored (mirrors the pattern already
established in `qa/features/util_isalive.feature`).

## Implementation (v2)

`packages/cb-gateway/src/cb_gateway/handlers/privacy.py`:

- `router.message(CommandName("privacy"))` — `CommandName` resolves via
  `cb_core.textmatch.parse_command`, which already maps `privacy` / `privacidade`
  / `privacidad` to canonical `"privacy"` and rejects `@OtherBot`-addressed
  commands outright (stricter than v1's bare prefix match — see Phase 6).
- No `FeatureGate`, no `AdminOnly` — matches v1's unconditional dispatch.
- `ctx = await context_for(bot, message)`; reply text is `t(ctx, "privacy")`.
- `message.reply(...)` — a reply to the triggering message, matching
  `msg_to_reply=msg`. `parse_mode` is left to the bot's default
  (`DefaultBotProperties(parse_mode=ParseMode.HTML)`, `qa/conftest.py:78-86`),
  equivalent to v1's explicit `parse_mode='HTML'`.
- No chat-action "typing" call: v2's gateway has no equivalent helper on this
  path yet and no other ported handler (`isalive.py`) sends one either; dropping
  a fire-and-forget cosmetic API call is not an observable regression for the
  QA scenario. Documented here rather than silently omitted.

## Phase 6 — Parity table

| Row | Verdict | Note |
|---|---|---|
| Triggers: `/privacy`, `/privacidade`, `/privacidad` | same | via `COMMAND_ALIASES` (already registered, not touched by this port) + `CommandName("privacy")`. |
| Trigger: `/privacy@CookieMWbot` | same | `parse_command` strips the `@target` and still resolves to `privacy` when addressed at *this* bot. |
| Trigger: `/privacy@OtherBot` | changed (intentional, fix) | v1's naive `startswith` would still fire (and reply) even when the command is addressed at a different bot sharing the group — Telegram delivers the update to every bot. v2's `parse_command` returns `None` for a foreign target, so the handler does not fire. This is the same fix already applied to every other v2 command (see `util_isalive.feature`'s "different bot" scenario) — preserving v1's bug here would mean two bots both replying to `/privacy@OtherBot`, which is strictly worse and not what any QA scenario asks for. |
| Trigger: bare prefix match (`/privacyland` etc.) | changed (intentional, fix) | v2 requires the parsed command name to equal `privacy` exactly (`parse_command` splits on whitespace/`@`, not on arbitrary suffix), so `/privacyland` no longer misfires. Not a behaviour any user or spec relies on. |
| Precondition: no admin gate | same | no `AdminOnly()` filter. |
| Precondition: not feature-flag gated | same | no `FeatureGate()` filter — always answers regardless of `functions_fun`/`functions_utility`. |
| Success output text | same | `t(ctx, "privacy")` reads the byte-identical ported string from `cb_core/locale_data/*/lib.json`. |
| Reply vs send | same | `message.reply(...)`, i.e. a reply to the triggering message, like v1's `msg_to_reply=msg`. |
| parse_mode HTML | same | via the bot's default properties rather than an explicit per-call argument; net effect identical. |
| Language selection (group chat) | same | `ctx.lang` comes from the group's configured `language` via `context_for`, same source v1 threads through as its `language` parameter in the group branch. |
| Language selection (private chat, hardcoded `'eng'`) | not ported | v2 has no private-chat dispatch branch at all yet (`handlers/__init__.py` only routes group-shaped updates through `CommandName` filters); porting v1's PV command menu (`COOKIEBOT.py:75-110`) is a separate, larger unit of work outside this feature's file ownership. Flagged for the owner of `handlers/__init__.py` / private-chat routing. |
| `send_chat_action('typing')` | changed (intentional, drop) | cosmetic-only; no other ported handler in this codebase sends one; not observable in the QA scenario. |
| Persistence | same | none. |
| Cooldown/quota | same | none. |
