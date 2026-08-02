# util_calladms — Design (DM half)

## Module placement

| Piece | Where | New/existing |
|---|---|---|
| Job-name constant | `packages/cb-core/src/cb_core/jobs.py` | existing file, new constant `CALLADMS_NOTIFY_ADMINS` |
| Worker job | `packages/cb-worker/src/cb_worker/jobs/calladms.py` | new, mirrors `cb_worker/jobs/everyone.py`'s shape |
| Worker registration | `packages/cb-worker/src/cb_worker/main.py` | existing file, add to `WorkerSettings.functions` |
| Gateway enqueue call | `packages/cb-gateway/src/cb_gateway/handlers/calladms.py` | existing file, replaces the `dm_fanout_not_implemented` log line |
| Unit tests (job) | `packages/cb-worker/tests/test_calladms_notify.py` | new, mirrors `test_everyone_fanout.py` |
| Acceptance | `qa/test_util_calladms.py` | existing file, `dm_confirmation` step rewritten |
| Contract | `docs/contracts/util_calladms.md` | existing file, "Decision" section closed out, Phase 6 table added |

No new database table, no migration — this feature has no persistence
(confirmed in both the original and this slice's contract table).

## Reply path vs. worker (R1)

**R1.1** The reply path (`confirm_call_admins` in `calladms.py`) keeps doing
exactly what it does today through the group ping, then makes one call:
`enqueue(jobs.CALLADMS_NOTIFY_ADMINS, group_id=..., chat_title=..., original_message_id=..., lang=...)`.
It does not resolve admin ids, does not touch Telegram beyond what the group
ping already does, and does not block on the job landing (`enqueue` never
raises, per `cb_gateway/queue.py`'s own contract).

**R1.2** All per-admin work — resolving who the admins are *right now*,
excluding the bot, sending each DM, throttling between sends — happens in
`cb_worker/jobs/calladms.py`, run by `cb-worker`. This is multi-chat fan-out
(AGENTS.md §2.4) exactly like `util_everyone`'s DM loop; it belongs off the
reply path for the same reason.

## Payload shape (R2)

**R2.1** Four scalars, matching `util_everyone`'s payload discipline (design
R4.7 there): `group_id: int`, `chat_title: str`, `original_message_id: int`,
`lang: str`.

**R2.2** No `admin_user_ids` and no `bot_id` in the payload, unlike the
original contract's "What the job needs" draft. Two reasons, both because the
job is not latency-sensitive and can afford a fresh resolve at DM time rather
than trusting a list resolved when the button was pressed:

- `cb_core.admins.admin_ids(bot, group_id)` is one cached, outage-resilient
  call the job can make itself — resolving it in the gateway and shipping the
  result through the broker would just be a second, redundant place admin
  membership could go stale between "enqueued" and "sent".
- `bot.id` needs no API call at all (`aiogram.Bot.id` is derived from the
  token) and `ctx["bot"]` in the worker is constructed from the *same* token
  the gateway's bot uses (`cb_core.bot.build_bot`, `cb_worker/main.py:startup`)
  — so the worker's `ctx["bot"].id` is identical to whatever the gateway would
  have computed and shipped.

**R2.3** No `group_id` vs. `chat_id` split, unlike `everyone_fanout`'s
payload. `util_everyone`'s job needs both because it re-reads
`group_members` (keyed by `group_id`, the DB distribution column) *and* calls
Telegram (keyed by the chat id) — two different systems that happen to share
a numeric space for a group but are conceptually distinct call sites. This
job never touches the database; `group_id` here is used exactly once, as the
argument to `cb_core.admins.admin_ids` and to the `'-100' in str(group_id)`
supergroup check — both Telegram-facing — so one field carries both roles
honestly.

## Job body (R3)

**R3.1** `notify_admins_of_call(ctx, *, group_id, chat_title, original_message_id, lang) -> None`,
the arq-registered entry point, wraps `_notify` in the same
span/`job_duration`/`log.exception` shape `everyone_fanout` uses — copied, not
imported (same reasoning `everyone.py`'s own docstring gives: `main.py`
imports this module to register it, so this module must not import back from
anything that imports `main.py`, and the wrapper has no import dependency on
`main.py` either way, but copying keeps every job module self-contained and
avoids inventing a shared wrapper for two call sites).

**R3.2** `_notify(bot, group_id, chat_title, original_message_id, lang) -> int`
(returns count sent, for the log line, mirroring `_fanout`'s return shape):

1. `admin_ids = sorted(await admins.admin_ids(bot, group_id))` — sorted for
   deterministic test assertions and log ordering; DM order has no user-visible
   meaning (each is an independent private chat).
2. `text = locale_get("notification_admin", lang, title=chat_title)`.
3. `keyboard` is `None` unless `"-100" in str(group_id)`, in which case it is
   one inline "Show message" button to `_deep_link(group_id, original_message_id)`
   — v1's exact substring test (`:199`), not aiogram's own supergroup-id
   convention, so a chat id shaped like a bare group id (no `-100` anywhere)
   gets no button, byte-identical to v1.
4. For each admin id except `bot.id`: `bot.send_message(admin_id, text, parse_mode="HTML", reply_markup=keyboard)`,
   individually wrapped — a raise (blocked, chat-not-started, deactivated
   account) is logged and counted, never aborts the remaining sends (D-CA-3).
   `asyncio.sleep(0.1)` between attempts (D-CA-4).

**R3.3** D-CA-2 (owner-forward every 10th DM) has no code path at all —
nothing to feature-flag, nothing to configure. Same precedent as
`util_everyone`'s D-EV-5.

## Telemetry (R4)

**R4.1** One counter, `cb_worker_calladms_dm_total{outcome}`, `outcome` in
`sent|blocked` — no `group_id`/`user_id` label (AGENTS.md §7). Narrower than
`everyone_dm_total`'s `sent|blocked|left|error`: this job never calls
`get_chat_member` (no registry hygiene — admins are resolved fresh via
`cb_core.admins`, which never raises, not read from a stored roster that
needs pruning), so neither `left` nor `error` has a code path that would ever
produce them.

**R4.2** `job_duration.labels(job="notify_admins_of_call", ...)` and
`log.info("calladms.notify_done", group_id=group_id, sent=sent)` on success,
via the shared wrapper (R3.1).

## Open decisions — answered

1. **Should the job re-resolve admins or trust a list the gateway already
   fetched?** Re-resolve (R2.2) — the job is not latency-sensitive, and a
   button press could sit in the queue behind other jobs; a stale admin list
   is a worse failure mode than one extra cached Telegram call.
2. **Keep the every-10th-DM owner forward, even gated behind a setting?** No
   (R3.3) — it was never disclosed, never configurable, and v2 has no concept
   of a bot-owner id to forward to. Dropping it is a permanent, deliberate
   divergence, same as `util_everyone`'s D-EV-5.
3. **Job name.** `notify_admins_of_call`, exactly the name the original
   contract's "What the job needs" section already proposed — no reason to
   invent a different one now that it is being built.
