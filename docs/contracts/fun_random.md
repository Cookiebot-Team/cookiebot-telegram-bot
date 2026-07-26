# Contract: fun_random (v1 -> v2)

Phase 2/6 of `/migrate-feature` for `/random` (`/aleatorio`). QA:
`../Cookiebot-QA/features/fun_random.feature`. FEATURE-MAP row: `fun_random`,
`/random`, aliases `/aleatorio`,`/random`, `SocialContent.py:198-206`,
`GET/POST /randomdatabase`, status `⚠ backend loads whole collection to pick 1`
(now fixed by the re-architecture below).

v1 is two separate pieces of code, both in `SocialContent.py`, plus one
dispatch site:

- **Read**: `random_media` (`SocialContent.py:198-206`) — up to 50 attempts of
  `GET randomdatabase` against the Java backend's `RandomDatabaseService.getRandom`
  (which loads the *entire* collection into the JVM to pick one row — the exact
  defect FEATURE-MAP already flagged), then a native `forwardMessage` of
  whatever `{id: chat_id, idMessage: message_id}` came back, into `thread_id`
  when the trigger was inside a topic. Any exception during an attempt (empty
  result, the source message since deleted, the source chat having banned this
  bot, ...) is swallowed and the loop just retries; if all 50 attempts fail,
  the function returns having sent nothing — silent, observable only as "the
  bot said nothing."
- **Write**: `add_to_random_database` (`SocialContent.py:191-196`), called from
  the dispatcher's photo/video branches (`COOKIEBOT.py:168-172`) only when
  `sfw and funfunctions and not publisherpost`. It additionally skips forwarded
  messages and groups whose title contains an NSFW-flagging substring, then
  remembers only `{chat_id, message_id, photo_file_id}` — **no bytes are ever
  downloaded in v1**; the "database" is a pointer to a still-live message, and
  `random_media` forwards that live message later.
- **Dispatch**: `COOKIEBOT.py:213-220`. `/random`/`/aleatorio`/`/aleatório`
  share one `elif` arm with a dozen unrelated fun commands
  (`/meme`, `/batalha`, `/idade`, ...), gated as a block:
  `if not funfunctions: notify_fun_off(cookiebot, msg, chat_id, language)`
  (`Miscellaneous.py:129-131`, text key `"fun_off"`); the specific
  `/aleatorio`/`/aleatório`/`/random` arm inside it calls `random_media(...)`.

## Phase 2 — v1 behaviour contract

| Aspect | v1 behaviour (file:line) |
|---|---|
| Triggers | `/random`, `/aleatorio`, `/aleatório` (`COOKIEBOT.py:213`). No `@botname` handling is v1-specific here (telepot's dispatch is plain prefix matching); this port's `CommandName("random")` filter handles the `@botname` suffix and both non-accented aliases already in `cb_core/textmatch.py`. |
| Preconditions | `if not funfunctions: notify_fun_off(...)` (`COOKIEBOT.py:218-219`) — group-level gate only, no admin check, no reply-required precondition. |
| Cooldowns / quotas | None. |
| Success output | A native Telegram forward (`forwardMessage`) of the original photo/video message into the *current* chat/topic — original caption and "Forwarded from" attribution intact, since it is a real forward, not a repost. |
| Failure output | None. An empty or exhausted pool (50 failed attempts) produces no reply at all — not an error message, not `fun_off`. |
| Persistence (write side) | `POST randomdatabase` with `{id: chat_id, idMessage: message_id, idMedia: photo_file_id_or_empty}` — a pointer, not a blob. Gated on `sfw and funfunctions and not publisherpost` (`COOKIEBOT.py:169,171`), plus (inside `add_to_random_database` itself) not-forwarded and title-not-NSFW-flagged (`SocialContent.py:192,194`). |
| Side effects | None beyond the forward itself. |
| External calls | `GET/POST randomdatabase` (Java backend, MongoDB-backed). |
| Known defects | FEATURE-MAP's own note: `RandomDatabaseService.getRandom` loads the whole collection into the JVM to pick one row — the read side is O(pool size) on every call. Fixed by the re-architecture below, not preserved. |

## Phase 2 — the re-architecture (media.py already committed to this; ported here)

`cb_core.storage.media.MediaService`'s own docstring already explains why: v1's
pool is one unbounded, cross-group Mongo collection with no dedupe, read by
loading every row into application memory — the exact anti-pattern Citus
punishes hardest. v2 makes the pool **per-group** (`media_objects.group_id`,
the distribution column, colocated with `groups`) and **content-addressed**
(`media_objects.content_hash`, deduped across groups at the blob layer)
— `/random` becomes a single-shard `ORDER BY random() LIMIT 1`
(`media.py`'s `_SELECT_RANDOM`), not a JVM-side full scan. This task's job was
only to feed that table and read it back — `MediaService.random()`/`put()`
were already implemented and tested before this port started; only the
handler (both the `/random` command and the write side that feeds it) was
missing.

Two consequences worth being explicit about, since they are real, observable
differences from v1 and not merely different code for the same behaviour:

1. **Scope**: v1's pool is global — `/random` in group A can return media
   originally posted in group B. v2's is per-group by construction — `/random`
   only ever returns something *this* group has itself posted. A brand-new
   group's pool is empty and stays empty until its own members post
   photos/videos. This is the FEATURE-MAP-flagged defect's fix, not a
   regression: v1's cross-group leak (private media from group A surfacing in
   unrelated group B) is exactly the kind of thing Citus's per-tenant model
   structurally prevents.
2. **Delivery**: v1 `forwardMessage`s the original, live message (Telegram
   shows "Forwarded from ..." and the original caption). v2 re-sends the
   stored bytes, preferring the original `file_id` when one was recorded (no
   re-upload — Telegram just re-serves its own cached copy) and falling back
   to the stored blob bytes otherwise. `media_objects` has no caption column
   and this port does not add one (`cb_core/*` and migrations are out of this
   task's file-ownership boundary), so a caption on the original post never
   reaches the resend, and there is no "Forwarded from" attribution. Flagged,
   not silently dropped — the correct fix is a caption column on
   `media_objects`, for whoever owns that migration next.

## Kinds pooled

Only `"photo"` and `"video"` — the exact two v1 branches
(`COOKIEBOT.py:168-172`) ever call `add_to_random_database` from.
`"animation"` is deliberately never written by this feature even though
`MediaService.random`'s default `kinds` tuple includes it (that default is
shared infrastructure for other future callers of the same table); v1's
animated-GIF/sticker reply path (`reply_sticker`, triggered by replying to the
bot) is a different feature and a different table (`add_to_sticker_database`),
out of scope here. `send_random_media` passes `kinds=("photo", "video")`
explicitly rather than relying on the default, so a future writer that starts
storing `"animation"` media for some other feature does not silently start
surfacing through `/random`.

## The `sfw` flag, both directions

v1's `sfw` config flag gates the **write** side only
(`COOKIEBOT.py:169,171`): the pool accumulates content exclusively from groups
configured safe-for-work, which is exactly what lets the read side
(`random_media`) forward without re-checking the *target* group's own `sfw`
flag — everything already in the pool is known-safe by construction. This port
keeps that write-side gate (`_should_pool`, `cb_gateway/handlers/fun_random.py`)
and additionally honours `sfw` again on the **read** side
(`_select_media`'s `sfw_only=ctx.config.sfw`): a group that later turns `sfw`
off is not artificially restricted to only-ever-safe content on read, since
nothing about the flag's *v1* meaning implies "even an unsafe-mode group must
only ever see pre-vetted content." In the steady state — every row ever
written by this feature has `sfw=True`, per the write gate — the two are
observably identical; the read-side filter only matters if some other future
writer or an ETL'd v1 row introduces an `sfw=False` row into this group's pool.

## Phase 3 — QA scenario

Copied `../Cookiebot-QA/features/fun_random.feature` verbatim (its one
scenario, unchanged) into `qa/features/fun_random.feature`, then added, to
cover v1 behaviour and this port's own re-architecture decisions the original
spec did not exercise:

- A scenario for `functions_fun` off (`COOKIEBOT.py:218-219`), asserting the
  `fun_off` text — the spec's one scenario never turns the gate off.
- A scenario for an empty/never-populated pool, asserting **no response at
  all** — v1's `random_media` silent-failure-after-50-attempts behaviour,
  which "the bot should respond with ..." (the spec's only assertion) cannot
  express by itself.
- A scenario proving the `sfw`-on read filter actually excludes an `sfw=False`
  row when one exists in the pool.
- A scenario proving a group with `sfw` off can still receive media that was
  collected before the switch (or by some other means) — the read-side flag
  documented above is a filter, not a second independent safety gate that
  would make an already-off group refuse everything.

## Phase 5 — Implementation (v2)

`packages/cb-gateway/src/cb_gateway/handlers/fun_random.py`,
`router = Router(name="fun_random")`:

- `pool_media` — `@router.message(F.chat.type != PRIVATE, F.photo | F.video)`.
  Bookkeeping only: always raises `SkipHandler`, so `core_mediarestrict`'s own
  photo/video handler (and any other router with an interest in the same
  update) still gets to run. Resolves the kind, checks `_should_pool` against
  the real `GroupConfig` (`sfw and functions_fun and not publisher_post`, plus
  not-forwarded and not-NSFW-titled), downloads the file's bytes through
  `Bot.download` (the one network call this port adds over v1 — see the
  re-architecture section), and calls `MediaService.put(..., sfw=True)`. Any
  failure anywhere in that path (download, storage) is caught, logged, and
  never escapes — a pooling failure must not take the reply path for anything
  else down with it.
- `send_random_media` — `@router.message(F.chat.type != PRIVATE,
  CommandName("random"))`. Not gated with `FeatureGate("fun")`: that filter
  answers nothing when the area is off, but v1 explicitly replies with
  `fun_off` for this exact command family — reproduced here as an explicit
  `ctx.enabled("fun")` check instead. Calls `MediaService.random(group_id,
  kinds=("photo", "video"), sfw_only=ctx.config.sfw)`; an empty result sends
  nothing (v1 parity). Otherwise sends via `answer_photo`/`answer_video`,
  preferring the stored `telegram_file_id` and falling back to the stored blob
  bytes (`BufferedInputFile`) only when no file id was ever recorded.

### Private chats

v1's dispatcher returns immediately for `chat_type == 'private'`
(`COOKIEBOT.py:73-105`) before any of the photo/video or command-dispatch code
this feature owns is ever reached — a `/random` typed in a DM instead falls
into v1's private-chat command chain, which has its own generic "Commands must
be used in a group chat!" fallback for any unrecognised `/` command
(`COOKIEBOT.py:103-104`). That generic private-chat fallback is not this
feature's code and is not reproduced here; both handlers in this file instead
filter out `ChatType.PRIVATE` entirely (`F.chat.type != ChatType.PRIVATE`), so
neither the pool nor `/random` ever fires in a DM, matching v1's *effective*
scope (this feature never runs in private in v1 either) without taking on a
generic private-command router that belongs to whichever feature ports that
fallback.

## Phase 6 — Parity table

| Row | Verdict | Note |
|---|---|---|
| Triggers: `/random`, `/aleatorio` | same | Both already in `cb_core/textmatch.py:COMMAND_ALIASES`. |
| Trigger: `/aleatório` (with accent) | **not built here** | v1 dispatches this spelling too (`COOKIEBOT.py:213`) but it is missing from `COMMAND_ALIASES`, which this task does not own. Needed in a file this task does not own — see "Needed elsewhere" below. |
| Feature-flag gate (`functionsFun`) | same, reproduced explicitly | v1: `if not funfunctions: notify_fun_off(...)` for this exact command family. This port checks `ctx.enabled("fun")` and sends `t(ctx, "fun_off")` directly rather than using `FeatureGate("fun")`, whose own docstring documents that it answers nothing when off — the generic shape does not match this command family's v1 behaviour. |
| `fun_off` text | same | Pre-existing in `cb_core/locale_data/{en,pt,es}/lib.json` (not written by this task); ported byte-for-byte, asserted in `qa/test_fun_random.py`. |
| Admin check | same (none) | v1 has none; this port adds none. |
| Success output: media type | same | photo/video only, matching what v1's write side ever pools. |
| Success output: delivery mechanism | **changed (intentional, re-architecture)** | Native `forwardMessage` (v1) -> re-send of stored bytes/`file_id` (v2). See "the re-architecture" above for the full comparison, including the caption/attribution loss this causes. |
| Success output: pool scope | **changed (intentional, fix)** | Global cross-group pool (v1, FEATURE-MAP-flagged defect) -> per-group pool (v2). A brand-new v2 group's `/random` is empty until its own members post; v1's would immediately return content from any other group in the deployment. |
| Failure output: empty/exhausted pool | same | No reply at all, not an error message — v1's 50-attempts-then-give-up behaviour, reproduced as `ref is None -> return`. |
| Write gate: `sfw and funfunctions and not publisherpost` | same | `_should_pool`, reproduced field-for-field against the real `GroupConfig`. |
| Write gate: forwarded messages excluded | same | `_is_forwarded` (`message.forward_origin`), the Bot-API-7.0-correct equivalent of v1's `forward_from`/`forward_from_chat` check. |
| Write gate: NSFW-titled groups excluded | same | `_NSFW_TITLE_SUBSTRINGS`, copied verbatim from `SocialContent.py:194`. |
| Read gate: `sfw` flag | changed (intentional, extended) | v1 only ever gates on write; this port also filters on read (`sfw_only=ctx.config.sfw`), a no-op in the steady state (see "The sfw flag, both directions" above) and a deliberate extra safety margin, never a new restriction on data v1 would have shown. |
| Kinds: `"animation"` never pooled | same (explicit) | v1 never writes it here either; this port passes `kinds=("photo","video")` explicitly rather than relying on `MediaService.random`'s broader default, so a future writer of `"animation"` media does not silently start surfacing through `/random`. |
| Persistence shape | changed (intentional, re-architecture) | v1: `{chat_id, message_id, photo_file_id}` pointer in Mongo, no bytes, no dedupe. v2: `media_objects` (distributed on `group_id`, colocated with `groups`) plus a content-addressed blob in `media_blobs`/the blob store — real bytes, deduped by content hash. Pre-existing schema and `MediaService`, not created by this task. |
| Private chats | same (effective scope) | v1 never reaches this feature's code in a DM (`COOKIEBOT.py:73-105`'s early return); this port filters `ChatType.PRIVATE` out explicitly rather than reproducing v1's generic "must be used in a group chat" fallback, which belongs to a different feature. |
| Thread/topic support | same | v1 forwards `into thread_id` explicitly; aiogram's `answer_photo`/`answer_video` shortcuts auto-fill `message_thread_id` from the triggering message, so no explicit handling was needed here. |
| Router wiring | **not built here** | `handlers/__init__.py` does not yet register `fun_random.router` — out of this task's file-ownership boundary, same gap several sibling ports (`core_rules`, `core_mediarestrict`) already flag. |

## Needed in files this task does not own

- `packages/cb-gateway/src/cb_gateway/handlers/__init__.py`:
  `root.include_router(fun_random.router)`. Until this is added,
  `qa/test_fun_random.py`'s scenarios that expect a live `/random` reply stay
  red end-to-end (the unit suite and the integration suite do not depend on
  this wiring and are green).
- `cb_core/textmatch.py:COMMAND_ALIASES`: add `"aleatório": "random"` (with the
  accent) alongside the existing `"aleatorio"` — v1 dispatches both spellings
  (`COOKIEBOT.py:213`) and only the unaccented one is present today.

## Test results (`uv run` from repo root)

- `ruff check` / `ruff format` — clean on all five owned files.
- `mypy packages/cb-gateway/src/cb_gateway/handlers/fun_random.py` — clean.
- `pytest -q -m "not integration" qa/test_fun_random.py
  packages/cb-gateway/tests/test_fun_random.py` — the unit suite
  (`test_fun_random.py`, 39 tests) is green and infra-free. The acceptance
  suite (`qa/test_fun_random.py`) needs a reachable Postgres (skips cleanly
  otherwise) for the real `media_objects` seeding and is red end-to-end today
  purely because of the router-registration gap above (four of five
  scenarios); the "empty pool -> no response" scenario already passes since
  nothing happening is also what an unregistered router produces, coincidentally.
- `pytest -q -m integration qa/integration/test_fun_random.py` — 13 tests,
  green against a real Postgres/Citus instance; skips cleanly when none is
  reachable.
