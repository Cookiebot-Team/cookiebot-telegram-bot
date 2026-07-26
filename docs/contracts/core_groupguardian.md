# Contract: core_groupguardian (v1 -> v2)

Phase 2/6 of `/migrate-feature` for the join captcha. QA:
`../Cookiebot-QA/features/core_groupguardian.feature`. FEATURE-MAP row:
`core_groupguardian`, status `⚠ state in flat Captcha.txt, no real lock`.

v1 handlers: `captcha_message`/`solve_captcha`/`check_captcha`/`parse_line_captcha`
(`../COOKIEBOT-Telegram-Group-Bot/Bot/GroupShield.py:231-345`), dispatched from
`../COOKIEBOT-Telegram-Group-Bot/Bot/COOKIEBOT.py:147-148` (join gate),
`:298-299` (reply-to-caption match), `:309-316` (catch-all fallback),
`:391-395` (callback buttons).

## What v1's captcha actually is (read before anything else)

A photo (`captcha.write(password, 'CAPTCHA.png')` — a 4-character numeric
password rendered as an image, from the characters `'0','2','3','4','5','6',
'8','9'`), sent as a reply to the join event, caption localised
(`captcha.title`), with three inline buttons: "ADMINS: Approve"
(`CAPTCHAAPPROVE`), "Call Admins" (`CAPTCHACALLADMIN`), "I'm not a Robot!"
(`CAPTCHASELF`). The answer is submittable by replying with text.

**Two real v1 defects, found by tracing the code, not inferred from the QA
prose:**

1. **The text-reply "verification" never checks the actual password.**
   `solve_captcha`'s text branch (`GroupShield.py:329-342`) is:
   ```python
   solveattempt = "".join(msg["text"].upper().split())
   if solveattempt.isnumeric() and len(solveattempt) == 4:
       ...  # succeeds
   ```
   `password` (the value written into the image) is never compared against
   `solveattempt` anywhere. Any 4-digit number — `"0000"`, `"1234"`, anything
   — passes, regardless of what the image showed.
2. **Both inline buttons that were supposed to prove humanity are
   unconditional free passes.** The button branch of `solve_captcha`
   (`GroupShield.py:317-328`) runs the same success path — no comparison of
   any kind — whether it was reached via `CAPTCHAAPPROVE` (an admin/owner
   pressing "Approve", `COOKIEBOT.py:391`, a legitimate override) or via
   `CAPTCHASELF` (`COOKIEBOT.py:391`, the newcomer pressing "I'm not a
   Robot!" **on their own challenge**, with no admin check at all).

So v1's real anti-bot value is "one click on a button, any button, by
anyone the dispatcher lets through" — the image is decorative. This is a
silent-failure bug (AGENTS.md `/migrate-feature` Phase 2: "Race conditions
and silent-failure bugs get fixed"), not a user-visible quirk worth
preserving, and it directly contradicts the QA scenario's stated intent
("fail to solve the captcha challenge correctly" implies a real check
exists). **Fixed in this port** using the already-compiled `cb_core.captcha`
module: `make_arithmetic()` for a real challenge with a real answer,
`verify()` (constant-time compare) for real verification, `callback_payload`/
`parse_callback` for the button wiring. The `CAPTCHAAPPROVE` admin-override
button is preserved (a legitimate designed feature, not a defect); the
`CAPTCHASELF` self-tap free pass is not (see Phase 6).

## Phase 2 — v1 behaviour contract

| Aspect | v1 behaviour (file:line) |
|---|---|
| Trigger | `new_chat_members` join event, **only when the joiner added themselves** — `msg['from']['id'] == msg['new_chat_participant']['id']` (`COOKIEBOT.py:142`, the `elif` sibling of the "someone else added them" branch at `:136`). An invited member never gets a captcha, only ever `welcome_message`. |
| Preconditions | `captchatimespan > 0` (`group_configs.captcha_timeout_seconds`, v1 default 300s, `Configurations.py:111`) **and** `myself['username'] in listaadmins` — the bot itself must be an admin of the group (`COOKIEBOT.py:147`). Also gated behind `util_doomlist`'s checks (`check_human`/`check_cas`/`check_banlist`/`check_banlist_public`) not already having kicked the joiner, and behind `check_raid`'s anti-raid ban not having fired (`GroupShield.py:118-138,232`) — both out of scope here, see Boundary. |
| Cooldowns / quotas | 5 attempts per challenge, hardcoded (`GroupShield.py:263`, the trailing `5` written into every `Captcha.txt` line), not configurable, not per-language. |
| Success output | v1: the image caption + 3 buttons (see above). v2 (fixed): the same localised caption text (`captcha.title`, byte-identical), plus the arithmetic prompt appended, plus one button per shuffled answer option, plus the admin-approve button. Sent as a reply to the join message. |
| Failure output (wrong text answer) | Hardcoded, **unconditionally Portuguese regardless of group language**: `"Senha incorreta, por favor tente novamente."` (`GroupShield.py:340` — no `language=` kwarg reaches `send_message`'s translate call). Preserved verbatim: a user-visible quirk, not a defect. |
| Failure output (attempts exhausted) | `captcha.limit` as the `reason`, substituted into `captcha.kick`, then the user is banned (`kickChatMember`) and **unbanned again 30 seconds later** (`threading.Timer(30, cookiebot.unbanChatMember, ...)`, `GroupShield.py:298-305`) — a temporary ban, not permanent. |
| Failure output (timeout) | Same kick/unban flow, `reason = captcha.time`. |
| Failure output (ban API call itself fails) | `captcha.error_kick` sent instead, no unban scheduled (there was nothing to unban) — `GroupShield.py:303-305`. |
| Reply-detection mechanism | Two v1 paths reach `solve_captcha`/`check_captcha`: (a) a reply whose `reply_to_message.caption` contains `f"{round(captchatimespan/60)} minutes"` or `"...minutos"` (`COOKIEBOT.py:298`, a fragile string match — with `funfunctions`-gated branches and several *other* more-specific `elif`s checked first); (b) the dispatcher's **final catch-all `else`** (`COOKIEBOT.py:309-316`) — reached for **any** unmatched, non-command message from anyone, which unconditionally calls `solve_captcha` (harmless no-op for a user with no pending row) whenever the captcha config gate is open. Net effect: a challenged user's *any* plain, non-command message is treated as a solve attempt, not only a reply to the captcha message specifically. |
| Persistence | `Captcha.txt`, a flat file rewritten in full on every join/solve/check, line format `CHATID userID yy-mm-dd hr:min:sec password captcha_id attempts`, no real lock (a `wait_open` sleep-and-retry helper, not a lock — FEATURE-MAP note). v2: `captcha_challenges(group_id, user_id, nonce, kind, answer, attempts, message_id, issued_at, expires_at, solved_at)`, PK `(group_id, user_id)`, distributed on `group_id`, colocated with `groups` (migration 0001). |
| Side effects | Restricts the newcomer (`restrictChatMember`, `can_send_messages: True` / media+other+previews `False`, `until_date`) while the challenge is pending (`GroupShield.py:236`) — **the same shape of mute** `core_mediarestrict` already re-architects around `group_members.joined_at` rather than a native Telegram restriction (see Boundary). |
| External calls | None beyond Telegram itself. |
| Known defects | The two described above (fixed, see Phase 6), plus flat-file persistence with no real lock (FEATURE-MAP row) — moot once persistence moves to Postgres. |

## Boundary with other features (explicitly out of scope here)

- **`util_doomlist`** (`check_human`/`check_cas`/`check_banlist`/
  `check_banlist_public`, `GroupShield.py:172-229`) runs *before* the captcha
  gate in v1 and can kick the joiner outright, in which case captcha never
  fires. Not ported anywhere in this codebase yet. When it lands, its router
  must be registered before `groupguardian.router` for the same
  join-priority reason as below, and it must raise `SkipHandler` when it did
  not act.
- **Anti-raid detection** (`check_raid`, `GroupShield.py:118-138`), called at
  the very top of `captcha_message` — a **global**, cross-group in-memory
  ledger banning every recent joiner across the whole bot process if too
  many joins land in a short window. This is architecturally a different
  concern (not per-`group_id`, no QA scenario covers it, no v2 schema exists
  for it) and is **not built here**. A group under a real raid still gets
  individually-captcha'd newcomers (correct, just slower than v1's batch
  ban) — not a regression in the observable per-user contract this feature
  owns, but flagged because a future anti-raid port needs its own table and
  its own contract.
- **`call_admins`** (`util_calladms`, `UserRegisters.py:168-203`) — v1's "Call
  Admins" captcha button. Not reimplemented; the button is dropped from the
  v2 keyboard.
- **Media restriction while pending** — v1's `restrictChatMember` call here
  is the *exact same mute* `core_mediarestrict` already owns
  (`group_configs.media_restrict_seconds`, re-architected in v2 around
  `group_members.joined_at` rather than a native Telegram restriction, per
  `docs/contracts/core_welcome.md`'s identical boundary section). Not
  called from this handler at all; left entirely to that feature.
- **`welcome_message` on a successful solve** — v1 calls it verbatim
  (`GroupShield.py:328,335`). This port reuses `cb_gateway.handlers.welcome`'s
  own `_welcome_text`/`_send_welcome_text` (imported, not duplicated) rather
  than reimplementing the placeholder-substitution contract that module
  already owns.

## Join-priority dependency (needed in a file this task does not own)

`groupguardian.on_join` and `welcome.on_join` both fire on `F.new_chat_members`.
v1 shows a captcha **instead of** the welcome message for a self-join with
the gate open; every other case (invited join, gate closed, bot-is-not-admin)
always gets `welcome_message`. `groupguardian.on_join` raises `SkipHandler`
in every one of those "not my case" branches so `welcome.on_join` can run —
**this only works if `groupguardian.router` is registered before
`welcome.router`** in `packages/cb-gateway/src/cb_gateway/handlers/__init__.py:
build_router` (not touched by this task; `docs/contracts/core_welcome.md`
already flags the identical dependency from the welcome side, and the
current `handlers/__init__.py` already uses this exact `SkipHandler`-ordering
convention for `mediarestrict` vs `stickerspam`). Until that registration
lands, no scenario in `qa/test_core_groupguardian.py` can pass end to end —
verified directly: a scratch build with `groupguardian.router` spliced in
before `welcome.router` was reverted before being committed, per this task's
file ownership, but every acceptance failure observed without it is
"the bot said nothing" (the join event never reached this handler at all),
not a logic defect.

## Reported gap: no proactive kick on timeout (cb-worker, not owned by this task)

v1's timeout enforcement is a `threading.Timer(captchatimespan+1, check_captcha,
...)` (`GroupShield.py:264-265`) that fires **on its own**, independent of any
further user activity, and kicks + unbans exactly as described above.
`packages/cb-worker/src/cb_worker/main.py:expire_captchas` is the v2
equivalent cron (`packages/cb-worker/src/cb_worker/main.py:129-142`) but it
only runs `DELETE FROM captcha_challenges WHERE solved_at IS NULL AND
expires_at < now()` — **it never bans the user, never sends a kick message,
and never schedules the 30s unban.** A user whose challenge expires and who
never sends another message is, today, simply left in limbo: never
welcomed, never kicked, and (once the cron sweeps the row) not even
retryable — a fresh join would re-arm the challenge via this handler's
upsert, but nothing prompts that fresh join.

This handler's own message-reply path (`on_captcha_text_reply`) partially
compensates, mirroring v1's real behaviour of re-checking expiry on the
*next* message rather than only on the timer (`check_captcha` is also called
from the dispatcher's catch-all on every message, `COOKIEBOT.py:316`): if an
expired-but-not-yet-swept user sends anything else, `_fail_attempt` notices
`expires_at` has passed and kicks them then. But a silent user is never
proactively kicked in v2. Closing this gap requires `expire_captchas` to
also ban + message + schedule the unban for every row it is about to
delete — **out of this task's file ownership** (`cb-worker` is explicitly
excluded). Flagged for whoever owns that file.

## Phase 3 — QA scenario

Copied `../Cookiebot-QA/features/core_groupguardian.feature` verbatim into
`qa/features/core_groupguardian.feature` (both scenarios unchanged), then
added:

- A wrong-answer-with-attempts-remaining scenario (not kicked yet).
- An attempts-exhausted scenario (5 wrong answers -> kicked).
- An admin-approve-override scenario.
- A newcomer-cannot-self-approve scenario (the fixed defect).
- An invited-join scenario (no captcha at all).
- A captcha-disabled scenario.
- A bot-not-admin scenario.

## Implementation (v2)

`packages/cb-gateway/src/cb_gateway/handlers/groupguardian.py`,
`router = Router(name="groupguardian")`:

- `on_join` — `@router.message(F.new_chat_members)`. `SkipHandler` for the
  bot's own join, for an invited (non-self) join, and whenever the gate is
  closed (`captcha_timeout_seconds <= 0` or the bot is not an admin).
  Otherwise generates a `cb_core.captcha.make_arithmetic()` challenge, sends
  it as a reply with one button per shuffled option plus an admin-approve
  button, and upserts `captcha_challenges`.
- `on_captcha_text_reply` — matches any non-command text from a sender with
  a pending row (the real, catch-all v1 behaviour, not only a reply to the
  captcha caption — see the "Reply-detection mechanism" row above). Deletes
  the user's message either way (v1 does too), then verifies.
- `on_captcha_callback` — `@router.callback_query(F.data.startswith("cap:"))`.
  The newcomer tapping one of their own options is a real verify; the
  newcomer tapping the admin-approve button has no effect (the fixed
  defect); an admin tapping admin-approve bypasses verification (preserved,
  legitimate v1 feature); anyone else tapping anything has no effect
  (matches v1's "no elif branch matches" outcome).
- `_succeed`/`_fail_attempt`/`_kick` — shared verdict logic for both entry
  points. Correct answer or admin-approve: deletes the row, calls
  `welcome._welcome_text`/`_send_welcome_text`. Wrong answer under the
  attempts cap: increments `attempts`, sends the hardcoded
  `WRONG_ANSWER_TEXT`. Attempts exhausted or past `expires_at`: bans, sends
  the localised kick/error-kick text, schedules a 30s unban
  (`asyncio.create_task`, matching v1's `threading.Timer` shape without
  blocking the reply path, AGENTS.md §4).
- Analytics: `event_type="captcha"` rows (`outcome="issued"/"solved"/
  "kicked_limit"/"kicked_time"/"kick_failed_*"`) via `cb_core.events.recorder()`,
  feeding `group_daily_stats.captcha_issued`/`captcha_solved`
  (migration 0001's rollup, previously unfed by any handler).

## Phase 6 — Parity table

| Row | Verdict | Note |
|---|---|---|
| Trigger: self-join only | same | `from_user.id == newcomer.id` check, `SkipHandler` otherwise. |
| Gate: `captcha_timeout_seconds > 0` and bot is admin | same | both conditions reproduced; `admins.is_admin(bot, group_id, bot.id)` is the v2 equivalent of `myself['username'] in listaadmins`. |
| Text-reply "verification" checks only shape, not the password | **changed (intentional, bug fix)** | v1 accepts any 4-digit number; v2 uses `cb_core.captcha.verify()` against the real generated answer. Documented at length above — this is the entire point of the port, not a side effect. |
| `CAPTCHASELF` self-tap free pass | **changed (intentional, bug fix)** | dropped entirely; a newcomer tapping the admin-only button has no effect. |
| `CAPTCHAAPPROVE` admin override | same (shape) | preserved as a real feature: an admin/owner can bypass verification for a newcomer. `ownerID`'s separate bot-owner bypass (`str(from_id) == str(ownerID)`) is not composed, same omission the `core_welcome`/`core_rules` ports already made for their own admin checks — `Settings.owner_id` exists but is otherwise unused anywhere in this codebase. |
| Attempts cap | same | 5, hardcoded, matching `GroupShield.py:263`. |
| Wrong-answer text | same (preserved quirk) | hardcoded Portuguese regardless of group language, byte-identical. |
| Kick + reason selection (limit before time) | same | `attempts >= MAX` checked before `expires_at`, matching v1's `if attempts <= 0: ... else: ...` inside an `or` guard. |
| Kick is temporary (30s auto-unban) | same | `asyncio.create_task` sleep-then-unban, same shape as v1's `threading.Timer`, non-blocking on the reply path. |
| Kick failure -> `error_kick` text, no unban scheduled | same | |
| "Any non-command message from a pending user is a solve attempt" | same | not narrowed to "must be a reply", matching v1's real catch-all behaviour (`COOKIEBOT.py:309-316`), not the narrower caption-substring branch alone. |
| Captcha image (OCR'd 4-digit photo) | **changed (intentional)** | no image compositing on the gateway's synchronous reply path (AGENTS.md §4); the localised caption text is preserved byte-for-byte, with the real arithmetic prompt appended so the (now-real) challenge is legible without an image. |
| Success -> `welcome_message` | same | reuses `welcome._welcome_text`/`_send_welcome_text` directly rather than duplicating that contract. |
| Media restriction while pending | **not built here** | `core_mediarestrict`'s responsibility, identical boundary to `core_welcome`'s. |
| Anti-raid ban (`check_raid`) | **not built here** | global, cross-group concern; no schema, no QA coverage; see Boundary. |
| `util_doomlist` pre-check | **not built here** | not yet ported anywhere in this codebase; ordering note left for when it lands. |
| "Call Admins" button | **not ported (deliberate)** | belongs to `util_calladms`, out of scope. |
| Proactive kick on timeout with no further activity | **known gap, not fixable here** | `cb-worker`'s `expire_captchas` only deletes the row; see the dedicated section above. |
| Persistence | same (re-architected) | `captcha_challenges`, PK `(group_id, user_id)`, every statement filters on `group_id`, upsert re-arms on rejoin (v1 has no such collision — flat file, no PK). |
| Join-router ordering (`groupguardian` before `welcome`) | **needed in a file not owned by this task** | see dedicated section above. |

## Needed in files I don't own

1. `packages/cb-gateway/src/cb_gateway/handlers/__init__.py:build_router` —
   add `root.include_router(groupguardian.router)` **before**
   `root.include_router(welcome.router)`. Every scenario in
   `qa/test_core_groupguardian.py` fails as "the bot said nothing" until this
   lands (verified: a temporary local splice, reverted before finishing this
   task, confirmed the wiring is the only missing piece).
2. `packages/cb-worker/src/cb_worker/main.py:expire_captchas` — currently
   only deletes expired rows; v1's real behaviour additionally bans the
   user, sends the localised kick text, and schedules a 30s unban. See the
   dedicated "Reported gap" section above.
3. When `util_doomlist` is ported, its router needs to be registered before
   `groupguardian.router` (same join-priority pattern), and its handler
   needs to raise `SkipHandler` when it does not act on a joiner.


## Durability gap: the 30s unban

A kicked newcomer is banned and unbanned 30 seconds later, matching v1's
`threading.Timer`. In v2 that is an `asyncio` task held in `_pending_unbans`
(a bare `create_task` was collectable mid-sleep, so the ban could have become
permanent — the rule warning about it had been suppressed).

It is still **in-process**: a gateway restart inside the 30s window loses the
unban and the user stays banned. The durable form is a deferred cb-worker job,
which needs gateway->worker enqueue wiring that does not exist yet — the same
missing piece `docs/contracts/util_calladms.md` needs for its admin DM fan-out.
Both are listed in HANDOFF.md's known gaps.
