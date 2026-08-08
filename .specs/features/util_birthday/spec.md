# util_birthday — Specify

> **Update (this slice): the missing caller was found.** The daily broadcast's
> trigger is `COOKIEBOT.py:333-339` — the message handler's `finally`, on the
> first update of a new UTC day — not a scheduler, which is why searching for
> one found nothing. The broadcast is built and the gap is closed; see
> `docs/contracts/util_birthday.md` §"The daily broadcast — the caller, found".

**Feature id:** `util_birthday` · **Milestone:** M2 · **Kind:** v1 port,
narrowed — see "Recommended scope" below
**v1 source:** `Bot/Birthdays.py:14-61` (`birthday`), dispatched
`Bot/COOKIEBOT.py:242-243`.

## Status: decisions approved, building

1. **Collection mechanism**: read whatever the importer carried over, no new
   DM collection UI. Approved — see §1.
2. **Scope**: the manual shape only (`/birthday`, `/nextbirthday`, matching
   both QA scenarios). Approved — see §2.
3. **Collage compositing**: Pillow; the 900s follow-up via
   `enqueue(..., _defer_by=900)`, not `threading.Timer`; photos via
   `cb_core.members.roster` + `bot.get_user_profile_photos`, no scrape.
   Approved — see `design.md`.

**One thing explicitly not resolved, by instruction — recorded as an open
parity gap, not a closed decision**: v1's `birthday()` also serves a second,
unattended, every-group shape (§2's "Cron"), driven by something outside
this checkout. This port does not build it — but its absence in this
checkout is not evidence of its absence in production. See "The unverified
daily broadcast — an open parity gap" below and `docs/contracts/util_birthday.md`.

## 1. The collection-mechanism question — settled

**v1's DM birthdate-collection code is dead. Confirmed, not inferred:**

- The only place `birthdate` is ever written anywhere in the v1 Python
  codebase is `check_new_name` (`UserRegisters.py:64-88`), specifically its
  `if chat_type == 'private': chat = cookiebot.getChat(chat_id); if
  'birthdate' in chat: ...` branch (`:73-80`).
- `check_new_name` has **exactly one call site in the entire repository**:
  `COOKIEBOT.py:332`, inside `thread_function`'s group-message tail —
  confirmed by `grep -rn "check_new_name" ../COOKIEBOT-Telegram-Group-Bot/Bot/*.py`,
  two matches, one definition and one call.
- That call site is **unreachable for a private chat**: `thread_function`'s
  `if chat_type == 'private':` block (`COOKIEBOT.py:73-110`) returns
  unconditionally at its end, and line 332 is deep inside the code that only
  runs *after* that block — for a group message, never a DM. Read the whole
  function start to finish to confirm this, not just the two call sites in
  isolation.
- The Java backend confirms independently: `UserResource`/`UserService`
  (`../COOKIEBOT-backend`) expose `birthdate` only as a **read** filter
  (`GET /users?birthdate=`, `UserService.findAll`'s month/day matching — the
  unindexable `$expr` scan HANDOFF already named); there is no dedicated
  "set birthdate" endpoint, only the generic `PUT /users/{id}` that
  `get_user_info` calls, itself reachable only through the same dead path.

So: no path in the live v1 bot writes a birthdate today, for anyone. Whether
any user has one at all depends entirely on whether this code was reachable
at some point in the past, before whatever refactor added the private-chat
early return.

**The importer already has a real, tested, working path for this data.**
`cb_worker/importer/mappers.py:map_users` already converts and carries
`birthdate` from v1's Mongo `users` collection into `users.birthdate`
(`_convert_birthdate`, handles the Spring Data JSR-310 BSON-datetime
encoding, an ISO string, or absence — already merged, part of the general
user-import path, not written for this feature). `0001_initial_schema.py`
already has the column, plus `birth_month`/`birth_day` as `GENERATED`
columns and the composite index HANDOFF already documented.

**Decision, per the brief's own framing ("collects through a path that
actually runs, or reads whatever the importer already carried over"): the
second.** There is no v1 *observable* behaviour to port for live collection
— code that has never run is not a behaviour, it is a diagram. `util_birthday`/
`util_nextbirthday` read `users.birthdate`, populated for migrated groups by
the importer (already built) and simply absent — `NULL`, already handled as
a safe, honest fallback per `_convert_birthdate`'s own docstring — for
anyone new. **No new DM collection UI is built in this port.** If live
collection is wanted later, it is a net-new feature (`/implement-feature`,
not `/migrate-feature`) built on `.specs/features/private_dispatch/`'s
mechanism, not a port of code that never executed.

## 2. The scope question — v1's `birthday()` does much more than "post today's list"

Read `Birthdays.py:14-61` in full, not just the manual-invocation slice.
One function serves **two** call shapes that share a loop body:

- **Cron** (no `msg`/`manual_chat_id`): iterates `get_request_backend('registers')`
  — every group the backend knows about — daily, presumably from some
  external scheduler (not found in this repo; `COOKIEBOT.py` never calls
  `birthday()` without `manual_chat_id`, so whatever drives the unconditional
  path is outside this checkout entirely, or has bit-rotted the same way the
  DM collection did).
- **Manual** (`/aniversario`, `/birthday`, `/cumpleanos`, dispatched
  `COOKIEBOT.py:242-243`, `manual_chat_id=chat_id`): `groups` becomes a
  one-element list containing only the invoking group — this is the shape
  QA's scenario actually exercises (`../Cookiebot-QA/features/util_birthday.feature`:
  "user sends the command /birthday" → "a montage of users that has their
  birthday on that day", one group, on demand — no cron, no "every group"
  language anywhere in the spec).

**Both shapes run the same body once a group qualifies**, and that body is
substantial:

1. **A real photo collage**, not a caption — `make_birthday_collage`
   (`Birthdays.py:60-79`): fetches each birthday person's photo via
   `get_profile_image` (`SocialContent.py:280-292`, the **same**
   `telegram.me`-scraping mechanism `fun_battle` already replaced —
   D-BT-2's twin, same fix applies: `cb_core.members.roster` + Bot API
   `get_user_profile_photos`, not the scrape), decodes each with OpenCV,
   arranges them in a grid, overlays a `Confetti.png` with alpha
   transparency, writes to a local file. **Real pixel compositing, not
   photo relaying** — unlike `fun_battle`, which only ever handed Telegram a
   `file_id` and never touched pixels, this needs the actual bytes
   downloaded and composited. AGENTS.md §2.4 names "image compositing"
   explicitly as `cb-worker` work, not reply-path.
2. **Pinning** — `cookiebot.pinChatMessage(...)`, best-effort
   (`try/except: pass`).
3. **A 🎂 message.**
4. **A 15-minute-deferred follow-up** — `threading.Timer(900, next_birthdays,
   ...)`. This is the exact defect class `core_stickerspam`'s in-process
   counter already was: state that must survive a restart, living in one
   process's memory instead. A deploy or a crash between the collage post
   and 900 seconds later silently drops the follow-up. **Fix, not
   preserve** — and there is a clean fix already sitting in this codebase:
   `cb_gateway.queue.enqueue`'s underlying `arq` pool supports a native
   `_defer_by` (a documented, reserved kwarg the enqueue wrapper already
   passes through, per its own docstring) — `enqueue(jobs.NEXT_BIRTHDAYS,
   ..., _defer_by=900)` is the same interval, durably.
5. **Manual mode still runs all four of the above** — the "should we post"
   condition is `(len(bd_users_in_group) and not is_new_birthday_pinned) or
   manual_chat_id` (`:44`), so `manual_chat_id` alone forces the post/pin/
   timer regardless of the pinned-message dedup check. Manual mode also lets
   the caller staple extra names onto the collage by tagging them in the
   command text (`msg['text'].split()` for `@`-prefixed tokens, `:41-42`),
   independent of whether they have a real `birthdate` on file at all.
6. **Pinned-message dedup** (`is_new_birthday_pinned`/`is_old_birthday_pinned`,
   `:32-33`) inspects the group's *current* pinned message caption for
   known celebratory phrases in three languages, to avoid re-posting the
   same day's collage twice. Only meaningful for the cron shape (posting
   the same day repeatedly, unattended); a human-invoked `/birthday` has no
   real "did I already do this today" question QA asks about.

## The unverified daily broadcast — an open parity gap

**This is not a closed decision.** If v1 really does post a daily birthday
montage to every live group via whatever scheduler drives the `manual_chat_id
= None` call shape, then shipping the manual command only is a **silent
regression** for every group currently receiving those posts — the feature
would read "done" on the progress board and be visibly missing from the
chat. This checkout has no evidence the scheduler exists (no cron entry, no
systemd timer, no `while True` loop anywhere in `../COOKIEBOT-Telegram-Group-Bot`
that calls `birthday()` unattended), but **absence of the caller in this
repo is not proof of absence in the running v1 deployment** — it could live
in infrastructure config, a separate script, or a host-level cron entry
none of the three reference repos would ever show.

This port builds the manual shape because that is what can be verified and
is what QA actually specifies — not because the daily broadcast has been
confirmed unnecessary. **Someone with access to the live v1 deployment needs
to confirm, before cutover, whether groups currently receive an unattended
daily birthday post.** If they do, that is a real, separate feature to
build before cutover, not an optional enhancement. Recorded here,
in `docs/contracts/util_birthday.md`, and in `HANDOFF.md` — not marked
resolved.

## Scope for this port (approved)

**Build**: the **manual** shape only — `/birthday`, `/aniversario`,
`/cumpleanos` and `/nextbirthday`, `/proximosaniversarios` as on-demand
commands, matching QA's actual scenarios. Within that:

- Collage generation, photo-sourced via `cb_core.members.roster` +
  `bot.get_user_profile_photos` (fun_battle's precedent), composited in
  **`cb-worker`** (new work, no compositing exists in v2 yet — needs an
  image library; **Pillow**, not OpenCV, is the recommendation: this
  codebase has no compositing dependency of either kind yet, and Pillow is
  the lighter, more idiomatic choice for "grid of photos plus one alpha
  overlay" than pulling in OpenCV's much larger surface for one feature —
  flagging the library choice explicitly since it is a new dependency, not
  deciding it silently).
- Pin (best-effort, matching v1).
- 🎂 message.
- The 900-second follow-up, via `enqueue(..., _defer_by=900)` instead of
  `threading.Timer` — fixed, not preserved, per AGENTS.md's silent-failure
  rule.
- Manual mode's "extra tagged names" addition to the collage, and its
  bypass of the pinned-message dedup check (there is no dedup check to
  bypass in this scope — see below).

**Not built, named follow-up**: the **cron** shape — daily, unattended,
every-group iteration (`get_request_backend('registers')`'s v2 equivalent
would be a full `groups` table scan), the pinned-message dedup check that
only matters for repeated unattended posting, and whatever external
scheduler v1 relied on (not found in this checkout — itself worth noting as
another possibly-bit-rotted mechanism, unverified either way). This is a
real, separate feature — "Cookiebot announces birthdays every day without
anyone asking" — not implied by anything QA specifies, and not needed for
the manual command to work correctly. If wanted, it is a `cb-worker` cron
job added later, reusing the same collage/roster/photo machinery this slice
builds.

## Behaviour contract (Phase 2) — the manual shape

| Aspect | v1 behaviour (file:line) |
|---|---|
| Triggers | `/aniversário`, `/aniversario`, `/birthday`, `/cumpleaños`, `/cumpleanos` (`COOKIEBOT.py:242-243`); `/proximosaniversarios`, `/nextbirthdays`, `/proximoscumpleanos` (`:244-245`) — same `elif` chain as `fun_death`/`fun_ship`, gated on `functionsFun` (`:218-219`) |
| Preconditions | `functionsFun` only — no admin check |
| Cooldowns / quotas | None — grepped `Cooldowns.py`, no entry |
| `/birthday` with no argument | `msg['text'].split()) == 1` ⇒ `bday.title` ("You need to type the usernames of today's birthday people!") and return — **no collage, no lookup at all** in this specific case (`:16-18`) — a real, easy-to-miss v1 quirk: a bare `/birthday` doesn't show *today's* birthdays, it asks the caller to name them |
| `/birthday <names>` or with existing known birthdays | Collage of: known members whose `birth_month`/`birth_day` match today **and** are in the group's roster, **plus** any `@name` tokens typed in the command (no birthdate needed for a manually-tagged name) |
| Collage caption | `make_birthday_caption` — a random `bday.cta` line (`%(names)s` joined with `" e "`) + `bday.closing` (`"\n\n<i>Happy birthday!</i>\n%(date)s"`) |
| `/nextbirthday` | Plain text, days 1-4 ahead, `bday.next` header (localised per-language via `i18n.get`, e.g. `en` "UPCOMING BIRTHDAYS (all groups):\n\n" — but the "(all groups)" wording itself is stale even for this port's single-group manual query, in every language — preserved, a cosmetic label mismatch, not a behaviour bug) then one line per day: `@username` or `firstName lastName`, or `"- \n"` if nobody that day |
| Persistence | None new — reads `users.birthdate`/`birth_month`/`birth_day` (already-migrated data) |
| External calls | Bot API: `get_chat_administrators`-independent — `get_user_profile_photos` per collage member, `pinChatMessage`, `sendPhoto`, `sendMessage`. No GCS, no `telegram.me` (redesigned away, `fun_battle` precedent) |
| Known defects | D-BD-1 (dead DM collection — a data-source decision, not a code fix, addressed in §1), D-BD-2 (in-process `threading.Timer`, fix via `_defer_by`), D-BD-3 below |

## D-BD-3 — a real crash in v1's own collage sizing, found while reading `make_birthday_collage`

`Birthdays.py:60-79` never resizes a fetched photo before pasting it. The
canvas size and every placement offset are computed from **each image's own
natural `shape`** (`collage_images[i].shape[0]`/`[1]`), while the overall
canvas is sized from only the **first** image's shape
(`collage_size = (height * collage_images[0].shape[0], width *
collage_images[0].shape[1])`). Two people's profile photos are essentially
never pixel-identical in size — a second photo larger than the first
produces a numpy assignment (`collage[y_start:y_end, x_start:x_end] =
collage_images[i]`) whose shapes don't match, which raises. This is not an
edge case; it is the common case whenever two real (non-placeholder) photos
of different resolutions appear in the same collage. **Fix, not preserve**
— a crash is not a behaviour a user could be relying on, and AGENTS.md's
Phase 2 rule is explicit that a silent-failure/crash bug gets fixed. Every
photo (and the placeholder) gets resized to one fixed cell size before
compositing.

## QA — one conflict found

`../Cookiebot-QA/features/util_nextbirthday.feature` matches v1 exactly, no
conflict.

`../Cookiebot-QA/features/util_birthday.feature`'s one scenario is a bare
`/birthday` with no argument, expecting "a montage of users that has their
birthday on that day." **v1 does not do that.** `birthday()`'s very first
check (`:16-18`) is `if manual_chat_id and len(msg['text'].split()) == 1:
reply bday.title; return` — a bare `/birthday` (exactly one token, the
command itself) always hits this branch and replies with the "you need to
type the usernames of today's birthday people!" prompt, **never** looking
up who actually has a birthday. Only `/birthday <anything else>` (any
second token at all, `@`-prefixed or not) reaches the real lookup.

Per AGENTS.md's tie-break (v1 code wins for observable behaviour, QA wins
for intent, conflicts recorded rather than silently resolved): the copied
`.feature` file keeps QA's wording unchanged, but the step bound to "the bot
should reply with a montage..." asserts what v1 **actually** does for a bare
`/birthday` — the prompt, not a montage. A net-new scenario
(`/birthday @someone`) covers the real montage-of-today's-birthdays path QA's
wording seems to have intended. Recorded here and in
`docs/site/content/docs/feature-map.mdx`, same pattern already used for
`fun_ship`'s lone-`@user1` conflict and `fun_battle`'s "tags another user."
