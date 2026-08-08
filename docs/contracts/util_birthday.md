# Contract: util_birthday (v1 -> v2)

Phase 2/6 of `/migrate-feature` for `/birthday`. QA:
`../Cookiebot-QA/features/util_birthday.feature`. FEATURE-MAP row:
`util_birthday`. Spec/design: `.specs/features/util_birthday/{spec,design,tasks}.md`.
Files owned by this port: `packages/cb-core/src/cb_core/birthdays.py` (new),
`packages/cb-core/src/cb_core/asset_data/birthday/*` (new),
`packages/cb-core/src/cb_core/jobs.py` (`BIRTHDAY_COLLAGE`,
`NEXT_BIRTHDAYS_FOLLOWUP`), `packages/cb-worker/src/cb_worker/collage.py`
(new), `packages/cb-worker/src/cb_worker/jobs/birthday.py` (new),
`packages/cb-worker/src/cb_worker/main.py` (registration),
`packages/cb-gateway/src/cb_gateway/handlers/birthday.py` (new), and the
tests listed below.

## Status: `done` — both of v1's shapes are built

v1's `birthday()` serves two invocation shapes that share a body: a manual,
on-demand command and an unattended, every-group daily broadcast. The manual
half shipped first, with the broadcast recorded here as an **unverified**
parity gap. **It is no longer unverified** — the caller was found, and the
broadcast is built. See "The daily broadcast" below, which replaces the
"unverified" section this document used to carry.

## Phase 1 — where v1 lives

- Handler: `birthday`, `Bot/Birthdays.py:14-61`.
- Collage: `make_birthday_collage`, `:60-79`. Caption: `make_birthday_caption`,
  `:90-96`.
- Dispatch: `COOKIEBOT.py:242-243` — always `manual_chat_id=chat_id`, i.e.
  every dispatch from the bot's own command handling is the manual shape.
- Locale strings: `bday.title`/`bday.cta`/`bday.closing`/`bday.next` —
  already ported byte-identical, `cb_core/locale_data/{en,pt,es}/lib.json`,
  a **nested** object (`cb_core.locales.get` cannot read it directly — see
  "Implementation" below).

## The collection-mechanism decision (approved)

v1's only birthdate-writing code (`check_new_name`'s private-chat branch,
`UserRegisters.py:73-80`) is dead: its one call site
(`COOKIEBOT.py:332`) is unreachable for a private chat, which returns
unconditionally before ever reaching it — confirmed by reading
`thread_function` start to finish, not just the two call sites in
isolation. The Java backend confirms independently: no "set birthdate"
endpoint exists anywhere. **This port reads whatever
`cb_worker/importer/mappers.py:map_users` already carried over from v1's
Mongo `users` collection** (already merged, not written for this feature) —
populated for a migrated group, `NULL` for anyone new. No new DM collection
UI is built; code that never ran is not a behaviour to be compatible with.
If live collection is wanted later, it is a net-new feature on
`.specs/features/private_dispatch/`'s mechanism, not a port.

## The daily broadcast — the caller, found

v1's `birthday()` also runs unattended, iterating every group the backend
knows about (`groups = get_request_backend('registers')` when
`manual_chat_id` is `None`), checking a pinned-message dedup marker so it
does not repost the same day twice. This document previously recorded that
"nothing in this checkout calls `birthday()` that way — no cron entry, no
systemd timer, no `while True` loop", and flagged the risk that a scheduler
living outside the three reference repos would make shipping only the manual
command a silent regression.

**There is a caller, and it is not a scheduler — which is why looking for one
found nothing.** `COOKIEBOT.py:333-339`, inside the `finally:` of the message
handler:

```python
finally:
    check_new_name(cookiebot, msg, chat_id, chat_type)
    if not is_alternate_bot and not current_date_mutex.locked():
        msg_date = ...                      # today, UTC
        with current_date_mutex:
            if current_stored_date != msg_date:
                current_date_utc = msg_date_utc
                birthday(cookiebot, current_date_utc, msg=msg)   # manual_chat_id=None
```

So the flagship process broadcasts to every group on the **first message it
happens to handle on a new UTC day**, off the back of an unrelated update.
Live v1 groups do receive an unprompted daily birthday post, and the gap this
section used to describe was real.

**v2 runs it as a cron, not as v1's trigger.** `broadcast_birthdays`
(`cb_worker/jobs/birthday.py`, registered at 00:10 UTC) reproduces the
*behaviour*, not the mechanism, because the mechanism has three properties
nobody wants: it fires late in a quiet group (whenever someone finally
speaks), it can fire twice if two processes race the module-global date, and
it never fires at all in a group whose day starts with silence — that group's
birthday post depends on a *different* group's traffic.

| v1 | v2 |
|---|---|
| first message of a new UTC day, flagship process only | `cron(broadcast_birthdays, hour=0, minute=10)` |
| `get_request_backend('registers')` — every group, then a member list per group | one `groups_with_birthdays(month, day)` query; a day with no birthdays does nothing at all |
| `time.sleep(3)` between groups on a worker thread (FEATURE-MAP **D8**) | one deferred job per group, `_defer_by = n * 3` — nothing blocks, and a crash mid-sweep loses only what was not yet enqueued |
| skip if `not funfunctions` (`:24-26`) | same, per group |
| skip if today's post is already pinned (`:32-33,44`) | same — `already_posted_today`, matching v1's three localised markers and the date |
| posts unprompted (no `reply_to`) | same; `post_birthday_collage`'s `message_id` is `None` for this shape |
| pins, sends `🎂`, schedules the 900s follow-up | same |

`CB_BIRTHDAY_BROADCAST_ENABLED` defaults to **true**, because v1 does this
today: switching it off by default would itself be the regression.

**Not ported:** v1's `is_old_birthday_pinned` flag (`:33`) is computed and
then used nowhere except a commented-out unpin (`:45-46`), so it has no
observable effect.

## Phase 2 — v1 behaviour contract (the manual shape, what this port builds)

| Aspect | v1 behaviour (file:line) |
|---|---|
| Triggers | `/aniversário`, `/aniversario`, `/birthday`, `/cumpleaños`, `/cumpleanos` (`COOKIEBOT.py:242-243`), gated on `functionsFun` (`:218-219`) |
| Preconditions | `functionsFun` only — no admin check |
| Cooldowns / quotas | None (`Cooldowns.py` grepped in full) |
| Bare `/birthday` | `len(msg['text'].split()) == 1` ⇒ `bday.title` prompt, **no lookup at all** (`:16-18`) — see "QA conflict" below |
| `/birthday <anything>` | Real lookup: today's birthday people **filtered to this group's roster**, plus any `@`-tagged extra names typed in the command (no birthdate needed for a manual tag; no deduplication against the real hits, `:41-42`, preserved) |
| Collage | Grid of photos (fixed-size cells after D-BD-3's fix) + confetti overlay, captioned with a random `bday.cta` line + `bday.closing`, sent as a reply, pinned (best-effort), followed by a bare 🎂 message |
| Persistence | None new — reads `users.birthdate`/`birth_month`/`birth_day` |
| External calls | Bot API only: `get_user_profile_photos` per collage member (no `telegram.me` scrape — `fun_battle`'s D-BT-2 precedent), `pinChatMessage`, `sendPhoto`, `sendMessage` |
| Known defects | D-BD-1 (dead DM collection — a data-source decision, addressed above), D-BD-2, D-BD-3 below |

## Defects — verdict per item

| id | Defect | Verdict |
|---|---|---|
| D-BD-1 | v1's DM birthdate collection is dead code | **not a code defect** — a data-source decision, see above |
| D-BD-2 | `threading.Timer(900, next_birthdays, ...)` (`:56-57`) — in-process memory; a restart between the collage post and 900s later silently drops the follow-up, same defect class `core_stickerspam`'s counter already was | **fixed** — `enqueue(jobs.NEXT_BIRTHDAYS_FOLLOWUP, ..., _defer_by=900)`, arq's native deferred execution, durable in Redis |
| D-BD-3 | `make_birthday_collage` never resizes a photo before pasting it; the canvas is sized from only the *first* image's shape while each image is placed at its *own* shape — two differently-sized real photos (the common case) makes the raw numpy assignment raise | **fixed** — every photo (and the placeholder) is resized to one fixed `256x256` cell before compositing; the grid math becomes safe by construction (`cb_worker/collage.py`) |

## QA — the recorded conflict

`../Cookiebot-QA/features/util_birthday.feature`'s one scenario is a bare
`/birthday`, expecting "a montage of users that has their birthday on that
day." **v1 does not do that** — the bare-argument branch above always
replies with the "type the usernames" prompt, never a montage. Per
AGENTS.md's tie-break (v1 code wins for observable behaviour, QA wins for
intent, conflicts recorded rather than silently resolved): the copied
`.feature` file keeps QA's wording unchanged, but the step bound to it
asserts v1's real behaviour (the prompt, no collage enqueued). A net-new
scenario (`/birthday @someone`) covers the real montage path. Recorded in
`docs/site/content/docs/feature-map.mdx`.

## Phase 6 — parity table

| Aspect | Verdict |
|---|---|
| Triggers, `functionsFun` gate | **same** |
| Bare `/birthday` → prompt, not a montage | **same, byte-identical** (the recorded QA conflict) |
| Target resolution (roster-filtered real hits + tagged extras, no dedup) | **same** |
| Photo source | **changed (intentional)** — roster + `get_user_profile_photos` replaces the `telegram.me` scrape, `fun_battle`'s D-BT-2 precedent |
| Collage sizing crash (D-BD-3) | **fixed** |
| Local temp-file race | **fixed** — no local file, composited bytes stay in memory (same class of fix as `fun_battle`'s D-BT-1) |
| 900s follow-up mechanism (D-BD-2) | **changed (intentional, fix)** — durable `_defer_by`, not `threading.Timer` |
| 900s follow-up *timing/content* | **same** — same interval, same target text |
| Caption, pin, 🎂 | **same** |
| Daily, every-group, unattended broadcast | **not built — open parity gap, unverified, not resolved** |

## Tests

| Layer | File |
|---|---|
| Unit — shared query, catalog reads | `packages/cb-core/tests/test_birthdays.py` |
| Unit — pure compositing, D-BD-3's fix proven directly | `packages/cb-worker/tests/test_collage.py` |
| Unit — target resolution, photo fallback, deferred scheduling, full collage flow | `packages/cb-worker/tests/test_birthday_job.py` |
| Unit — trigger surface | `packages/cb-gateway/tests/test_birthday.py` |
| Acceptance — QA's scenario (asserting v1's real bare-argument behaviour) + the real-montage net-new scenario + the fun-off gate | `qa/features/util_birthday.feature`, `qa/test_util_birthday.py` |

No integration test: the group-scoped query is structurally identical to
`cb_core.members.roster`'s, already asserted single-shard by
`qa/integration/test_everyone.py`.
