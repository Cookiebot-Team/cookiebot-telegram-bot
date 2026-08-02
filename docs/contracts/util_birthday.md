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

## Status: `partial` — read this before assuming the feature is done

**This is not the whole feature. It is the part that could be verified.**
v1's `birthday()` serves two invocation shapes that share a body: a manual,
on-demand command (what this port builds, matching both QA scenarios
exactly) and an unattended, every-group daily broadcast driven by something
outside `../COOKIEBOT-Telegram-Group-Bot` entirely. See "The unverified
daily broadcast" below — **do not treat `partial` here as "everything except
some polish."**

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

## The unverified daily broadcast — an open parity gap, not a closed one

v1's `birthday()` also runs unattended, iterating every group the backend
knows about (`groups = get_request_backend('registers')` when
`manual_chat_id` is `None`), checking a pinned-message dedup marker so it
does not repost the same day twice. **Nothing in this checkout calls
`birthday()` that way** — no cron entry, no systemd timer, no `while True`
loop anywhere in `../COOKIEBOT-Telegram-Group-Bot` invokes it unattended.

**Absence of the caller in this repository is not proof of absence in the
running v1 deployment.** If v1's live groups currently receive a daily,
unprompted birthday montage from a scheduler that lives in infrastructure
config, a separate script, or a host-level cron entry none of the three
reference repos would ever show, then shipping only the manual command is a
**silent regression** for every one of those groups at cutover — the
feature reads "shipped" on this project's own board while a real,
currently-working behaviour quietly disappears from the chat.

**This is not resolved by this port, and is not resolved by the absence of
evidence in this checkout.** Someone with access to the live v1 deployment
needs to confirm, before cutover, whether groups currently receive an
unattended daily birthday post. If they do, building the daily-broadcast
shape (a `cb-worker` cron job, reusing this port's collage/roster/photo
machinery, over every group in `groups` rather than one) is a real,
necessary piece of work — not an optional enhancement — and belongs in
`HANDOFF.md`'s gap list until it is either built or confirmed unnecessary.

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
