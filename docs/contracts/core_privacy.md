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
the spec didn't cover: the Portuguese/Spanish aliases, the `@botname` form, a
command addressed at a different bot being ignored (mirrors the pattern already
established in `qa/features/util_isalive.feature`), and — landed alongside
`.specs/features/private_dispatch/` — `/privacy` in a private chat, which
upstream QA has no scenario for at all.

## Implementation (v2)

`packages/cb-gateway/src/cb_gateway/handlers/privacy.py`, two handlers on one
router:

- `privacy_private` — `router.message(F.chat.type == ChatType.PRIVATE,
  CommandName("privacy"))`. Replies `locales.get("privacy", "en")` directly,
  matching v1's hardcoded `'eng'` private-chat branch
  (`COOKIEBOT.py:87-88`). No `context_for` call — see "The private-chat fix"
  below.
- `privacy` — `router.message(F.chat.type != ChatType.PRIVATE,
  CommandName("privacy"))`, unchanged from before this port: `ctx =
  await context_for(bot, message)`; reply text is `t(ctx, "privacy")`.
- `CommandName` resolves via `cb_core.textmatch.parse_command`, which already
  maps `privacy` / `privacidade` / `privacidad` to canonical `"privacy"` and
  rejects `@OtherBot`-addressed commands outright (stricter than v1's bare
  prefix match — see Phase 6) — unchanged, applies to both handlers.
- No `FeatureGate`, no `AdminOnly` on either — matches v1's unconditional
  dispatch in both chat kinds.

### The private-chat fix (`.specs/features/private_dispatch/`)

Before this landed, this file had **one** handler with no chat-type filter
at all — a private-chat `/privacy` matched it and called `context_for`,
which reads `group_id = message.chat.id` and queries `group_configs`
(distributed on `group_id`) with a DM's own chat id, a "group" that never
existed. Not hypothetical: reproduced, then fixed. See
`cb_gateway/private_context.py`'s module docstring and
`.specs/features/private_dispatch/spec.md` ("The live bug") for the full
story — `core_listcommand.md` already had the pattern that fixes it
(`ChatType.PRIVATE` never calls `context_for` at all), this is that same
fix applied here.
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
| Language selection (private chat, hardcoded `'eng'`) | same | `privacy_private` replies `locales.get("privacy", "en")` directly, matching v1's hardcoded value without needing a group lookup — ported via `.specs/features/private_dispatch/`. Fixed a live bug along the way: before this, a DM `/privacy` fell through to `context_for`, which queried `group_configs` with a private chat's id (never a real group). `/start`'s full PV menu (`COOKIEBOT.py:75-110`) remains a separate, larger, named follow-up — this port is only `/privacy`'s own DM branch. |
| `send_chat_action('typing')` | changed (intentional, drop) | cosmetic-only; no other ported handler in this codebase sends one; not observable in the QA scenario. |
| Persistence | same | none. |
| Cooldown/quota | same | none. |
