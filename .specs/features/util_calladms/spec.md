# util_calladms — Specify (DM half only)

**Feature id:** `util_calladms` · **Milestone:** M2 · **Kind:** v1 port (completion)
**v1 source:** `Bot/UserRegisters.py:178-203` (`call_admins`), dispatched from
the `ADM` callback branch `Bot/COOKIEBOT.py:396-408`.

## Goal

The group-ping half of `/adm` is done (`packages/cb-gateway/src/cb_gateway/handlers/calladms.py`,
`docs/contracts/util_calladms.md`). This slice finishes the feature: v1's
`call_admins` also DMs every admin individually. That was blocked on
gateway->worker enqueue, which `util_everyone` built
(`cb_gateway/queue.py`, `cb_core/jobs.py`, `cb_core/bot.py` +
`ctx["bot"]` in `cb_worker/main.py`, `cb_worker/jobs/everyone.py` as the
worked example). Nothing else about the already-shipped group ping changes.

## Scope

In: a `cb-worker` job that DMs each resolvable admin, the gateway enqueue call
that replaces the `calladms.dm_fanout_not_implemented` log line, the job-name
constant, worker registration, tests at unit/acceptance layers, contract and
docs close-out.

Out: any change to the confirmation prompt, the staleness window, the group
ping text, or the callback-answer fix — all already shipped and already
covered by `packages/cb-gateway/tests/test_calladms.py` and
`qa/test_util_calladms.py`.

## Behaviour contract (Phase 2) — the DM half

| Aspect | v1 behaviour (file:line) |
|---|---|
| Trigger | Same as the group ping: reached only after a confirmed `/adm`/`/admin`/`/report`/`@admin`/`@adm` press. No separate trigger of its own. |
| Preconditions | None beyond "confirmed". v1 has no permission check on the DM step (`call_admins`, `UserRegisters.py:178-203`) — it runs for whichever admin list it was handed. |
| Cooldowns / quotas | None. `Cooldowns.py` has no entry (already confirmed in the existing contract). |
| Admin source | v1: the `listaadmins` (usernames) captured earlier in `thread_function_query`, then re-resolved one at a time via `GET users?username={username}` per admin (`:191`) — an N+1 backend lookup, same shape already flagged for `util_everyone` (D-EV-1). |
| Success output (per admin) | A DM: `i18n["notification_admin"]` ("You were called in the chat <b>{title}</b>", `%(title)s`), `parse_mode='HTML'` (`:198`). A "Show message" inline URL button linking to `https://t.me/c/{chat_id without the -100 prefix}/{message_id}`, **only** when `'-100' in str(chat_id)` — i.e. only for supergroups; otherwise no `reply_markup` at all (`:199`). |
| Exclusions | The bot's own account is skipped: `int(user[0]['id']) == int(myself['id'])` (`:192`). An admin whose username does not resolve to exactly one backend user is also skipped (`:192`) — no v2 equivalent needed, since v2 resolves ids directly via `cb_core.admins.admin_ids`, never by username lookup. |
| Side effects | `time.sleep(0.1)` between DMs (`:201`). Each send individually wrapped in a bare `try/except Exception: pass` (`:202-203`) — a blocked/blocking-not-started-chat admin is silently skipped, not reported anywhere. |
| Known defect | Every 10th successful DM forwards the triggering group message to a hardcoded bot-owner id (`:195-196`, `forwardMessage(ownerID, chat_id, message_id)`). Undisclosed exfiltration of group content: no group configuration controls it, no group member is told, no v2 equivalent exists anywhere (`ownerID` is not a concept in v2 at all). **Verdict: drop**, not preserved and not replaced — identical reasoning and precedent to `util_everyone`'s D-EV-5 (`docs/contracts/util_everyone.md`). |
| Persistence | None. No table, no write. |
| External calls | v1: one Telegram `getChat`/`getMe` (reused from the group-ping half) plus one `GET users?username=` per admin and one `sendMessage`/`forwardMessage` per admin. v2: `cb_core.admins.admin_ids(bot, group_id)` (one cached Telegram call, shared with every other admin-gated feature) plus one `sendMessage` per resolved, non-bot admin. |

## QA

No separate scenario exists for the DM half beyond the one already in
`../Cookiebot-QA/features/util_calladms.feature` ("And should send a message
on the adm's DM confirming that they have been pinged in a group"), already
copied into `qa/features/util_calladms.feature` verbatim. Its step definition
(`qa/test_util_calladms.py::dm_confirmation`) currently asserts the opposite —
that no DM call happens — documenting the gap this slice closes. It is
rewritten to assert the fan-out job is now enqueued with the right arguments
(the same proxy `qa/test_util_everyone.py` uses for its own worker fan-out:
mock the broker, not a real DM, since the actual send happens in a process
this suite does not run). No wording in the `.feature` file changes — the
Gherkin step text still reads "should send a message on the adm's DM
confirming...", true in effect once the enqueued job runs in `cb-worker`.

## Preserve / fix verdicts

| id | Defect | Verdict |
|---|---|---|
| D-CA-1 | N+1 username->id backend lookup per admin (`:191`) | **fix** — `cb_core.admins.admin_ids` resolves ids directly, no per-admin round trip |
| D-CA-2 | Every-10th-DM forward to a hardcoded owner id (`:195-196`) | **drop** — undisclosed exfiltration, no v2 concept of an owner id, same call already made for `util_everyone`'s identical D-EV-5 |
| D-CA-3 | Each DM silently swallowed on any exception (`:202-203`) | **preserve** — "blocked by user" is the routine outcome, not an error; the exact behaviour `util_everyone`'s fan-out already codifies as the house pattern |
| D-CA-4 | `0.1s` throttle between DMs (`:201`) | **preserve** — reasonable as-is, matches `util_everyone`'s fan-out throttle exactly |
| D-CA-5 | Bot excluded from its own DM loop, not from the group mention text (`:192` vs. the group ping, which the shipped half already preserves) | **preserve** — already correct in the shipped group-ping half; the DM half exclusion uses `bot.id` (no API call, derived from the token) rather than a username comparison |

No QA/v1 conflict beyond the one already recorded in the existing contract
(the DM step's wording predates this slice and is unchanged).
