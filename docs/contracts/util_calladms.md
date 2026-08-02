# Contract: `util_calladms` (v1 -> v2)

Phase 2/6 of `/migrate-feature` for the `/adm` "summon the group's admins"
flow. The group-ping half and the DM half were ported in two sessions; the DM
half's own spec/design/tasks live in `.specs/features/util_calladms/`. Files
owned by the DM half: `packages/cb-core/src/cb_core/jobs.py`
(`CALLADMS_NOTIFY_ADMINS`), `packages/cb-worker/src/cb_worker/jobs/calladms.py`
(new), `packages/cb-worker/src/cb_worker/main.py` (registration),
`packages/cb-gateway/src/cb_gateway/handlers/calladms.py` (the enqueue call),
and the tests listed in the Tests table below.

## Phase 1 — where v1 lives

- Dispatch: `COOKIEBOT.py:274-275` — `elif msg['text'].startswith(("/adm", "@admin", "@adm", "/report")): call_admins_ask(cookiebot, msg, chat_id, language)`.
  This `elif` is a direct sibling of every other top-level command branch inside
  `if msg['text'].startswith("/") and len(msg['text']) > 1:` (`COOKIEBOT.py:186`)
  — it is **not** nested inside the `if not utilityfunctions: notify_utility_off(...)`
  guard that gates dice/giveaway/youtube/etc. two branches down
  (`COOKIEBOT.py:248-263`). `/adm`, `@admin`, `@adm` and `/report` fire
  regardless of the group's `functionsUtility` setting, even though
  `docs/site/content/docs/feature-map.mdx` files this feature under "Util".
- Prompt: `UserRegisters.py:168-176` `call_admins_ask`.
- Ping + DM fan-out: `UserRegisters.py:178-203` `call_admins`.
- Callback handling: `COOKIEBOT.py:396-408`, the `elif query_data.startswith('ADM'):` branch
  inside `thread_function_query`.
- Aliases: `cb_core/textmatch.py:51` already maps `adm`/`admin`/`report` -> `calladms`
  (out of this port's file ownership; not touched here).
- Cooldowns: none. `Cooldowns.py` has no entry for `/adm`/`call_admins` — checked
  by grep across the whole file; the only anti-abuse anywhere near this feature
  is the 600-second staleness window on the confirmation button itself.

## Phase 2 table

| Aspect | v1 behaviour (file:line) |
|---|---|
| Triggers | `/adm`, `@admin`, `@adm`, `/report` (raw `str.startswith`, not word-bounded — `/administrator` would also match `/adm`) — `COOKIEBOT.py:274`. The leading-`/` forms are aliased in `cb_core/textmatch.py` (`adm`/`admin`/`report` -> `calladms`); the bare-word `@admin`/`@adm` forms are matched by `_MENTION_TRIGGER` in the handler, since `parse_command` only inspects `/`-prefixed text. Word-bounded, so `@admins`/`@adminfoo` do not fire where v1's raw `startswith` did. |
| Preconditions | None. No admin gate on who may *ask* to call admins (`call_admins_ask` has no permission check at all), no `functionsUtility` gate (see Phase 1), no group/private-chat check inside the handler itself (v1's private-chat fallback lives in the shared dispatcher preamble, `COOKIEBOT.py:75-110`, outside this feature's code). |
| Reply required? | No. `/adm` acts on the message that invoked it (used only to build the "Show message" deep link inside the *DM*, never inside the group ping), not on a message it replies to. |
| Cooldowns / quotas | None (checked `Cooldowns.py` in full — no entry). The only time-based gate is the confirmation button's own 600-second staleness window (`COOKIEBOT.py:401`), measured from the **confirmation prompt's** own `date`, not the original `/adm` message's. |
| Success output (prompt) | Reply to the `/adm` message with `i18n["call_admin_ask"]` ("Do you confirm to call the admins?" / pt "Confirma chamar os administradores?" / es "¿Confirma llamar a los administradores?"), two inline buttons `✔️` / `❌`, `callback_data` = `f"ADM Yes {language} {msg['message_id']}"` / `f"ADM No {language} {msg['message_id']}"` (`UserRegisters.py:168-176`). |
| Success output (confirmed) | The confirmation prompt is deleted unconditionally (`COOKIEBOT.py:400`, before the age check). A **new**, non-reply group message: `" ".join(f"@{u}" for u in listaadmins)` + `i18n["call_admin"]` (`"\n%(caller)s calling all admins!"`), `parse_mode='HTML'` (`UserRegisters.py:178-184`). `caller` = the presser's `username`, falling back to `first_name` if they have none — **no leading `@`** on the caller name itself, unlike the mention list. `listaadmins` is **not** filtered to remove the bot's own username — only the DM loop skips the bot (`UserRegisters.py:192`), so the ping text includes `@<bot's own username>` whenever the bot itself is an admin of the group (true for almost every real deployment). Then, for every admin whose username resolves to exactly one backend user record and who is not the bot itself: a DM `i18n["notification_admin"]` ("You were called in the chat <b>{title}</b>"), with a "Show message" URL button linking to `https://t.me/c/{chat_id without -100}/{message_id}` **only** when the chat id contains `-100` (i.e. only for supergroups) — throttled `time.sleep(0.1)` between sends, and every 10th admin also gets the original `/adm` message forwarded to the bot owner (`UserRegisters.py:186-203`). |
| Cancelled output | `i18n["canceled"]` ("Command canceled" / pt "Comando cancelado" / es "Comando cancelado") sent as a **new**, non-reply group message (`COOKIEBOT.py:407-408`). |
| Stale-button output | The confirmation prompt is still deleted first, then `answerCallbackQuery(query_id, text="Message too old, use /adm again")` — a bare English literal, **never routed through `i18n.get`**, so it never localises regardless of group language (`COOKIEBOT.py:401-403`). No group message is sent in this branch. |
| Callback-answer bug | v1 never calls `answerCallbackQuery` for the Yes/No press itself (only for the stale branch) — the presser's Telegram client spins its loading indicator forever on a successful confirm or cancel (`COOKIEBOT.py:404-408`). |
| Persistence | None. No table, no backend call specific to this feature. |
| Side effects | Group ping (single chat, this port implements it) + **N DMs, one per admin** (`UserRegisters.py:190-203`) — a distinct Telegram chat per admin, not the group — plus an occasional `forwardMessage` to the bot owner. |
| External calls | `get_request_backend(f"users?username={username}")` per admin, to resolve a username to a Telegram user id for the DM (`UserRegisters.py:191`) — an N+1 backend call pattern, same shape FEATURE-MAP already flags for `util_everyone`. |
| Known defects | No cooldown/anti-abuse at all (not a v1 defect list item — this command was simply never rate limited). The "not gated by `functionsUtility` despite being filed under Util" quirk is preserved as v1 behaviour, not fixed — fixing it would be a deliberate behavioural *change*, not a port. All four v1 triggers are live. |

## The DM half is a cb-worker job (`cb_worker/jobs/calladms.py`)

The DM step in `call_admins` (`UserRegisters.py:178-203`) sends a message to a
**distinct Telegram chat per admin** — not the group the command was run in —
throttled with `time.sleep(0.1)` and an occasional `forwardMessage` to the bot
owner. That is exactly the "fan-out to many chats" case AGENTS.md section 2.4
names as something that must never run on the reply path (`cb_gateway/main.py`'s
own docstring repeats the same rule). A group can have anywhere from one to a
few dozen admins; sequential DMs plus the 100ms throttle is unbounded work
sitting inside a webhook handler that Telegram expects to return quickly or
redeliver.

The **group ping is different**: it is one `sendMessage` call to the chat the
command already came from — no different from any other reply-path handler in
this codebase (e.g. `rules.py`, `config_menu.py`) — so it stays in the handler.

This was left as a named follow-up when the group-ping half shipped, blocked
on gateway->worker enqueue not existing yet. `util_everyone` built that
wiring (`cb_gateway/queue.py`, `cb_core/jobs.py`, `cb_core/bot.py` +
`ctx["bot"]` in `cb_worker/main.py`); this feature is its second consumer.
`.specs/features/util_calladms/` has the full spec/design for what follows.

### What the job actually needs — and what it re-derives itself

`confirm_call_admins` (`packages/cb-gateway/src/cb_gateway/handlers/calladms.py`)
enqueues `jobs.CALLADMS_NOTIFY_ADMINS` with four scalars only:

- `group_id: int` — the group whose admins to DM (same value as the Telegram
  chat id; this job never reads the database, so there is no separate
  DB-vs-Telegram id split the way `util_everyone`'s payload has).
- `chat_title: str` — for the DM text (`notification_admin`, `%(title)s`).
- `original_message_id: int` — to build the "Show message" deep link, only
  when `group_id` is a supergroup (its id contains `-100`, v1's exact
  substring test, reproduced byte-for-byte rather than "starts with").
- `lang: str` — the group's resolved language.

**Not** in the payload, unlike the earlier draft above: `admin_user_ids` and
`bot_id`. Both are cheap for the job to derive itself, and deriving them late
is more correct, not just more convenient:

- `cb_core.admins.admin_ids(bot, group_id)` is one cached, outage-resilient
  Telegram call the job makes directly — the job is not latency-sensitive,
  so trading a few hundred milliseconds for "whoever is an admin right now"
  beats shipping a list that could already be stale by the time the job runs.
- `bot.id` needs no API call (`aiogram.Bot.id` is derived from the token) and
  `ctx["bot"]` in the worker is built from the same token the gateway's bot
  uses (`cb_core.bot.build_bot`), so it is identical either way.

Throttling is `asyncio.sleep(0.1)` between sends inside the job, same value
v1 used. The owner-forward-every-10th behaviour
(`UserRegisters.py:195-196`) is **not** reproduced — see the Defects section
below; it is dropped outright, not merely optional.

## Defects — verdict per item (DM half)

| id | Defect | Verdict |
|---|---|---|
| D-CA-1 | N+1 username->id backend lookup per admin (`:191`) | **fix** — `cb_core.admins.admin_ids` resolves ids directly, no per-admin round trip |
| D-CA-2 | Every-10th-DM forward to a hardcoded owner id (`:195-196`) | **drop** — undisclosed exfiltration of group content, no configuration, no v2 concept of an owner id; identical reasoning and precedent to `util_everyone`'s D-EV-5 |
| D-CA-3 | Each DM silently swallowed on any exception (`:202-203`) | **preserve** — "blocked by user" is the routine outcome, not an error |
| D-CA-4 | `0.1s` throttle between DMs (`:201`) | **preserve** |
| D-CA-5 | Bot excluded from its own DM loop only, not from the group mention text (`:192`) | **preserve** — already correct in the shipped group-ping half; the DM job excludes `bot.id` (no API call) rather than comparing usernames |

## Policy decided for v2

1. **No cooldown added.** v1 has none for this command; adding one would be a
   behavioural change, not a port.
2. **Callback is always answered**, on every branch (confirmed, cancelled,
   stale). v1's failure to do so on the confirmed/cancelled branches is the same
   spinner bug already fixed the same way in `config_menu.py`
   (`docs/contracts/util_config.md`) — preserved as a fix, not as a quirk,
   for consistency with that precedent.
3. **The group-mention list is fetched directly from Telegram** (`bot.get_chat_administrators`),
   not through `cb_core.admins`. That module's `Admin` dataclass
   (`docs/contracts/admins.md`) deliberately carries only `user_id`/`role`/privilege
   flags — nothing in M1 before this port needed a username — so it cannot answer
   "what do I `@mention` this admin as" without a second Telegram round trip per
   admin. One direct call here, mirroring v1's own `get_admins` shape exactly,
   is simpler and no slower than round-tripping through a cache that cannot
   answer the question anyway. Reported to the `cb_core.admins` owner as a
   possible future enhancement (a `username: str | None` field on `Admin`), not
   fixed here since `cb_core/admins.py` is out of this port's file ownership.
   A Telegram failure here degrades to "no admins mentioned" (logged) rather
   than propagating — v1's own `get_admins` has no failure handling at all and
   would drop the whole update silently; failing softer here is strictly
   better and costs nothing a real deployment would notice (this path only
   runs when a human has already pressed "confirm").
4. **`@admin`/`@adm` bare-word triggers are implemented** — by a local
   `_MENTION_TRIGGER` regex in the handler, not by `parse_command`, which only
   inspects `/`-prefixed text and must keep doing so: treating `@admin` as a
   command there would capture every mention of a user named "admin". One
   deliberate narrowing against v1, whose bare `startswith` also matched
   `@admins` and `@adminfoo`: the regex is word-bounded, so those do not fire.
5. **The `functionsUtility` non-gating is preserved**, not "fixed" to match its
   FEATURE-MAP category. v1's actual code never checks the flag for this
   command; QA's spec is silent on it either way, so v1 code wins per
   AGENTS.md's tie-break rule.
6. **The DM job re-resolves admins from `cb_core.admins` rather than trusting
   a list captured when the button was pressed.** Symmetric with policy #3's
   choice for the group ping, but the opposite trade-off on purpose: the
   group ping is latency-sensitive (a fresh, uncached Telegram call is worth
   it to avoid a second round trip for usernames the cache doesn't carry),
   while the DM job has no such constraint and a stale admin list is a worse
   failure mode than one more cached lookup.

## Phase 6 — parity table (DM half)

| Aspect | Verdict |
|---|---|
| DM sent to every resolvable admin | **same** — `notification_admin`, `parse_mode='HTML'` |
| Admin resolution | **changed (intentional)** — one cached `cb_core.admins.admin_ids` call instead of v1's N `GET users?username=` lookups (D-CA-1) |
| Bot excluded from the DM loop | **same** — `bot.id` comparison replaces v1's `int(user[0]['id']) == int(myself['id'])`, same effect, no API call needed either way |
| "Show message" button, supergroup-only | **same, byte-identical condition** — `'-100' in str(group_id)`, v1's exact substring test, not aiogram's "starts with -100" convention |
| Deep-link URL construction | **same** — `_deep_link` replaces v1's `.replace('-100', '')` with `.removeprefix`, same output for every id Telegram actually sends (D-CA-1 in `util_everyone`'s contract makes the identical correction for its own deep link) |
| Throttle between DMs | **same** — `0.1s` |
| Each DM individually failure-suppressed | **same** — "blocked by user" is the routine outcome in both (D-CA-3) |
| DM fan-out location | **changed (intentional)** — v1 DMs from the same handler thread that answered the confirmation; v2 enqueues `jobs.CALLADMS_NOTIFY_ADMINS` with scalars only and returns, the fan-out runs in `cb-worker` (AGENTS.md §2 "nothing slow on the reply path") |
| Every-10th-DM owner forward | **dropped (D-CA-2)** — undisclosed exfiltration, no v2 equivalent, same call `util_everyone`'s D-EV-5 already made |

## Tests (DM half)

| Layer | File |
|---|---|
| Unit — deep-link URL, `-100`-substring button gating, bot-id exclusion, empty-admins degrade, a raising send not aborting the loop | `packages/cb-worker/tests/test_calladms_notify.py` |
| Acceptance — the existing QA "DM confirming" scenario, rewritten to assert the fan-out job is enqueued with the right arguments (mock broker, same pattern `qa/test_util_everyone.py` uses) | `qa/features/util_calladms.feature`, `qa/test_util_calladms.py` |
