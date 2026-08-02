# Contract: core_listcommand (v1 -> v2)

Phase 2/6 of `/migrate-feature` for `/commands`. QA:
`../Cookiebot-QA/features/core_listcommand.feature`. FEATURE-MAP row:
`core_listcommand`, status `✅`.

## Phase 1 — where v1 dispatches this

v1 handler: `list_commands`,
`../COOKIEBOT-Telegram-Group-Bot/Bot/Miscellaneous.py:124-127`:

```python
def list_commands(cookiebot, msg, chat_id, language):
    send_chat_action(cookiebot, chat_id, "typing")
    string = i18n.get_file("Cookiebot_functions.txt", lang=language)
    send_message(cookiebot, chat_id, string, msg_to_reply=msg)
```

Dispatched from two places in
`../COOKIEBOT-Telegram-Group-Bot/Bot/COOKIEBOT.py`:

- Private chat, `COOKIEBOT.py:85-86` — `elif msg['text'].startswith(("/comandos",
  "/commands")): list_commands(cookiebot, msg, chat_id, 'eng')`. Hardcoded
  English; there is no group to read a language or a gate from.
- Group chat, `COOKIEBOT.py:276-277` — `elif msg['text'].startswith(("/comandos",
  "/commands")): list_commands(cookiebot, msg, chat_id, language)`. This `elif`
  arm sits in the same chain as the fun-functions block (`:214-247`, gated by
  `if not funfunctions: notify_fun_off(...)`) and the utility-functions block
  (`:248-263`, gated by `if not utilityfunctions: notify_utility_off(...)`), but
  is its **own, later, ungated** `elif` — reached only if none of the earlier
  arms matched, and with no gate check of its own.

**What a gate being off actually does (read, not guessed):** `funfunctions`/
`utilityfunctions` are checked *inside* the fun/utility command blocks
themselves (`:218`, `:252`) — when off, the specific fun or utility command the
user typed is refused with `notify_fun_off`/`notify_utility_off`
(`Miscellaneous.py:129-135`, text keys `"fun_off"`/`"utility_off"`). Neither gate
is consulted anywhere in `list_commands` or its dispatch arm. So: **a gate being
off hides nothing from `/commands`** — the full static text lists every command
regardless, and the user only discovers a command is off by trying it.

## Phase 2 — v1 behaviour contract

| Aspect | v1 behaviour (file:line) |
|---|---|
| Triggers | `msg['text'].startswith(("/comandos", "/commands"))` — prefix match, group: `COOKIEBOT.py:276-277`; private: `:85-86`. |
| Preconditions | None. No admin check. **Not gated** by `functionsFun`/`functionsUtility` — see above; the arm is unconditional in both chat kinds. |
| Cooldowns / quotas | None. |
| Success output | `i18n.get_file("Cookiebot_functions.txt", lang=language)` — the whole file, verbatim, sent as a reply to the triggering message (`msg_to_reply=msg`), default `parse_mode='HTML'`. Group chat: `language` is the group's configured language. Private chat: hardcoded `'eng'`, regardless of the sender's own language or Telegram `language_code`. Byte-identical copies now live at `cb_core/locale_data/{en,pt,es}/Cookiebot_functions.txt`, read via `cb_core.locales.text("Cookiebot_functions", lang)` — never retyped or reformatted for this port. |
| Failure output | None — no failure branch; the file read always succeeds (bundled at build time in both v1 and v2). |
| Persistence | None. |
| Side effects | `send_chat_action(cookiebot, chat_id, 'typing')` before the reply (cosmetic "typing…" indicator). |
| External calls | None. |
| Known defects | None specific to this handler. Inherits none of FEATURE-MAP's D1-D13. |

### What "per-tenant filtering" means on top of v1 (the v2 addition)

v1 has no concept of a command catalog or of a command being switched off for a
whole brand: which commands exist is a compile-time fact of one Python process,
and a different persona (`is_alternate_bot`, FEATURE-MAP `core_botskins`) means a
*separate process* with its own copy of the dispatcher, not configuration.

v2 introduces two reference tables that give a brand on the shared `"core"`
handler pack a way to turn a command off **without a code change**:

- `command_catalog` (`packages/cb-api/migrations/versions/0001_initial_schema.py`)
  — one row per command, global `enabled` kill switch. Seed row:
  `('commands', 'core', false, NULL, 'core_listcommand')` — `enabled` takes its
  SQL default, `true`.
- `tenants.disabled_commands` (`cb_core/tenancy.py`, `0003_tenants.py`) — a text
  array per tenant; `Tenant.command_enabled(name)` is `name not in
  disabled_commands`.

`cb_gateway.handlers.listcommand.command_available_for_tenant(row, tenant)` is
the pure function that combines them: unavailable if the catalog row is missing
or disabled, else `tenant.command_enabled(row["command"])`. This is deliberately
**orthogonal to `functions_fun`/`functions_utility`** — those remain per-group
booleans on `group_configs` that gate the fun/utility *commands themselves*
(unchanged from v1); per-tenant filtering is a second, independent axis that
gates whether `/commands` (or any other cataloged command) exists **at all** for
the brand the message arrived through, resolved from the `skin` the dispatcher
already threads through the pipeline (`cb_gateway.main:111`,
`tenancy.registry.by_skin`).

**Why a single-tenant deployment produces exactly v1's output:** the seeded
`'cookiebot'` tenant carries no `disabled_commands`, and the seeded
`command_catalog.commands` row is `enabled = true`. With those two defaults,
`command_available_for_tenant` is `True` unconditionally, so `/commands` behaves
exactly as v1's ungated dispatch — the new axis is a no-op until someone
explicitly configures a tenant otherwise. Both reads also fail open (see
`_commands_available`'s docstring): a catalog or tenant-registry outage never
hides the help text, the same "must never go silent" posture AGENTS.md §2.6
asks of analytics, extended here to this read.

## Phase 3 — QA scenario

Copied `../Cookiebot-QA/features/core_listcommand.feature` verbatim into
`qa/features/core_listcommand.feature`, then added scenarios v1's code supports
that the spec didn't cover: the Portuguese alias `/comandos` and a command
addressed at a different bot being ignored (mirrors the pattern already
established in `qa/features/util_isalive.feature` and
`qa/features/core_privacy.feature`).

**Not added as a Gherkin scenario:** "the fun/utility gates being off does not
hide the list." Proving it needs a real `group_configs` row with both gates
turned off, and `qa/test_core_listcommand.py`'s harness has no database (no
`qa/integration/conftest.py` fixtures reachable from `qa/`) and must not
monkeypatch `cb_core.group_config`'s internals to fake one — AGENTS.md §6, "No
mocking of our own code in acceptance tests." It is instead a DB-backed test in
`qa/integration/test_command_catalog.py`
(`TestSingleTenantParity.test_functions_gates_off_does_not_hide_the_command_list`).

## Implementation (v2)

`packages/cb-gateway/src/cb_gateway/handlers/listcommand.py`:

- `router.message(CommandName("commands"))` — `CommandName` resolves via
  `cb_core.textmatch.parse_command`, which already maps `commands`/`comandos` to
  canonical `"commands"` (aliases pre-existing, not touched by this port) and
  rejects `@OtherBot`-addressed commands.
- No `FeatureGate`, no `AdminOnly` — matches v1's unconditional dispatch.
- `_commands_available(skin)` resolves the tenant for the bot the update arrived
  through (`tenancy.registry.by_skin`, already cached and already fail-open) and
  the `command_catalog` row (`_fetch_catalog_row`, a plain reference-table PK
  read), then applies `command_available_for_tenant`. A catalog read failure
  fails open (`True`) rather than hiding the reply.
- Private chat: `list_commands_private`, its own
  `router.message(F.chat.type == ChatType.PRIVATE, CommandName("commands"))`
  handler (originally an inline `if message.chat.type == ChatType.PRIVATE`
  branch inside the one handler below; relocated, not rebehaviored, when
  `.specs/features/private_dispatch/` generalised the pattern this file
  invented first). Replies with `locales.text("Cookiebot_functions", "en")`
  directly — no `context_for` call, matching v1's hardcoded `'eng'` and the
  fact that there is no group to look a config up for. This was already the
  one place in the codebase that got the private-chat case right on its own
  (unlike `docs/contracts/core_privacy.md`, which had a live bug until the
  same slice fixed it) — skipping `context_for` entirely for a DM avoids the
  only real risk (asking Telegram for a private chat's "administrators",
  which `cb_core.admins` already degrades gracefully from, but which is
  simpler to just never call).
- Group chat: `ctx = await context_for(bot, message)`; reply text is
  `locales.text("Cookiebot_functions", ctx.lang)`.
- `message.reply(...)` — a reply to the triggering message, matching
  `msg_to_reply=msg`. `parse_mode` is left to the bot's default
  (`DefaultBotProperties(parse_mode=ParseMode.HTML)`), equivalent to v1's
  implicit HTML default.
- No `send_chat_action('typing')` call: confirmed against the mock Telegram
  server (`qa/mock_telegram.py`) that `sendChatAction` is not in its handled
  method list, so it falls through to a bare `{}` result — which aiogram's
  `Response[bool]` model rejects with a `ClientDecodeError` (reproduced directly
  against the mock before writing this handler). Same call already dropped for
  the same reason of "no other ported handler sends one" in `core_privacy.md`;
  here it is additionally the harness itself that cannot serve it without a
  change to `qa/mock_telegram.py`, which this feature does not own.

## Phase 6 — Parity table

| Row | Verdict | Note |
|---|---|---|
| Triggers: `/commands`, `/comandos` | same | via `COMMAND_ALIASES` (pre-existing) + `CommandName("commands")`. |
| Trigger: `/commands@CookieMWbot` | same | `parse_command` strips the `@target` and still resolves when addressed at *this* bot. |
| Trigger: `/commands@OtherBot` | changed (intentional, fix) | v1's `startswith` would still fire even addressed at a different bot in the group (Telegram delivers to every bot present); `parse_command` returns `None` for a foreign target. Same fix already applied to every other v2 command (`util_isalive.feature`, `core_privacy.feature`). |
| Precondition: no admin gate | same | no `AdminOnly()` filter. |
| Precondition: not feature-flag gated | same | no `FeatureGate()` filter — the list always shows regardless of `functions_fun`/`functions_utility`, verified in `qa/integration/test_command_catalog.py`. |
| Precondition: per-tenant filtering | v2-only (new axis, no-op for a single tenant) | `command_available_for_tenant` — `True` for the seeded `'cookiebot'` tenant + seeded catalog row, so observably identical to v1 until a tenant is explicitly configured otherwise. Not something v1 could ever exhibit; documented rather than silently added. |
| Success output text | same | `locales.text("Cookiebot_functions", lang)` reads the byte-identical ported file. |
| Reply vs send | same | `message.reply(...)`, i.e. a reply to the triggering message, like v1's `msg_to_reply=msg`. |
| `parse_mode` HTML | same | via the bot's default properties rather than an explicit per-call argument. |
| Language selection (group chat) | same | `ctx.lang` from the group's configured `language` via `context_for`, same source v1 threads through as `language`. |
| Language selection (private chat, hardcoded `'eng'`/`'en'`) | same | handler special-cases `ChatType.PRIVATE` to `locales.text(..., "en")` directly, matching v1's hardcoded value without needing a group lookup. |
| `send_chat_action('typing')` | changed (intentional, drop) | cosmetic-only; breaks the mock Telegram harness (`ClientDecodeError`, reproduced before this decision) and, per `core_privacy.md`, no other ported handler sends one either. |
| Persistence | same | none. |
| Cooldown/quota | same | none. |
