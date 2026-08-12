# Contract: fun_death (v1 -> v2)

Phase 2/6 of `/migrate-feature` for `/death`, `/morte`, `/muerte`. QA:
`../Cookiebot-QA/features/fun_death.feature`. FEATURE-MAP row: `fun_death`.
Files owned by this port: `packages/cb-gateway/src/cb_gateway/handlers/death.py`,
`packages/cb-gateway/src/cb_gateway/handlers/__init__.py` (one router line),
`packages/cb-gateway/tests/test_death.py`, `qa/features/fun_death.feature`,
`qa/test_fun_death.py`, this file. `cb_core.legacy_assets` (the asset accessor
this feature reads) and `cb_worker.bucket_export`/`cb.py legacy-catalog` (the
prerequisite that unblocked it) belong to their own slices — see
`packages/cb-core/src/cb_core/legacy_assets.py`'s module docstring.

## Phase 2 — v1 behaviour contract

v1: `death`, `../COOKIEBOT-Telegram-Group-Bot/Bot/Miscellaneous.py:335-357`.
Dispatch: `COOKIEBOT.py:216,218-219,238-239`.

| Aspect | v1 behaviour (file:line) |
|---|---|
| Triggers | `/death`, `/morte`, `/muerte` — `startswith` prefix match, shared fun `elif` chain (`COOKIEBOT.py:216,238-239`). Aliases already lived in `cb_core/textmatch.py:COMMAND_ALIASES` before this port started; confirmed, not re-declared. |
| Preconditions | `functionsFun` gate shared by every command in that chain — off replies with `fun_off`, no admin/membership check (`COOKIEBOT.py:218-219`). |
| Cooldowns / quotas | none |
| Target resolution | ① more than one whitespace token in the message -> the raw second token, un-resolved (`msg['text'].split()[1]`) ② else a reply -> the replied-to sender's first name ③ else the caller's own `@username`, or bare first name with no username (D-DE-1's dropped skull prefix) |
| Success output | ① react `👻` ② `sendChatAction upload_photo` unconditionally (D-DE-2) ③ pick one random blob from the `Death` prefix ④ build the caption: skull-prefixed target + a randomised `death.template` variant + a randomised `death.Reason` line ⑤ send as an animation if the filename ends `.gif`, else a photo, both as a reply |
| Failure output | none — an empty blob list crashes with `ValueError` (D-DE-3) |
| Persistence | none |
| Side effects | one signed-URL round trip to GCS per call, v1 only |
| External calls | v1: GCS `generate_signed_url` + Telegram `sendChatAction`/`sendAnimation`/`sendPhoto`. v2: no GCS — `cb_core.legacy_assets` + `cb_core.storage`, same shape `fun_complaint`/`fun_meme` already use. |
| Known defects | D-DE-1, D-DE-2, D-DE-3 below |

### Defects — verdict per item

| id | Defect | Verdict |
|---|---|---|
| D-DE-1 | `'💀💀💀 ' + '@'+username if 'username' in msg['from'] else first_name` parses as `('💀💀💀 @'+username) if has_username else first_name` — no username means no skull-emoji prefix at all. | **preserve** — user-visible quirk, same category as `fun_ship`'s `@@alice + @@bob`. `resolve_target`'s `skip_skull_prefix` return reproduces it exactly, asserted in `test_death.py`. |
| D-DE-2 | `sendChatAction upload_photo` sent unconditionally even for a `.gif`, which goes out via `sendAnimation`. | **preserve** — cosmetic, not worth a divergent code path. `death.py`'s `_deliver` sends the chat action before the pool pick even knows the extension, matching v1's own order. |
| D-DE-3 | No empty-pool fallback: `random.randint(0, len(bloblist_death)-1)` on an empty list raises `ValueError`, uncaught. | **fixed.** `legacy_assets.choose` returns `None` for an empty pool instead of crashing; `_deliver` logs a warning and sends nothing further. Reachable today (a checkout that has not run `cb.py legacy-catalog`), unlike in v1 where the bucket was never actually empty in production. |

## Why this was blocked, and what unblocked it

v1's image pool was a live listing of a private GCS bucket
(`bloblist_death = list(storage_bucket.list_blobs(prefix="Death"))`,
`Miscellaneous.py:17`), never checked into any repository — `.specs/features/fun_death/spec.md`'s
"The blocker" section is the investigation that found nothing to vendor.
`cb_worker.bucket_export` has since copied every v1 prefix, `Death/` included,
into `cb_core.storage` under content-addressed keys, and `cb.py legacy-catalog`
turns the export manifest into the small per-prefix catalogs
`cb_core.legacy_assets` reads. `Death/` is 34 objects, 21.5 MB — past what
belongs in the wheel the way `fun_complaint`'s 3.4 MB is vendored — so this
port follows `fun_meme`'s split (a tiny catalog as package data, the bytes in
object storage) rather than `fun_complaint`'s (bytes vendored whole).

## The gif/photo decision reads `source_path`, not the storage key

v1 branches on `fileblob.name.endswith('.gif')` — the object's path inside the
bucket. `LegacyAsset.source_path` carries that; `LegacyAsset.storage_key`
(`.destination_key`) happens to carry the same extension today too, because
`bucket_export.keys.destination_key` derives it from the same source name at
export time — but that is the exporter's implementation, not a contract this
handler should lean on. `is_gif(entry.source_path)` reads the field whose
actual meaning is "v1's original filename". The comparison is lower-cased,
unlike v1's literal `.endswith('.gif')`: no v1-observable behaviour depends on
an extension's case, and a case-sensitive check could silently misroute a
`.GIF` export into `reply_photo`.

## Phase 6 — parity table

| Aspect | Verdict |
|---|---|
| Triggers (`/death`, `/morte`, `/muerte`, bare/tagged/with `@botname`) | **identical** — all three resolve to the canonical `death` name via `cb_core/textmatch.py:COMMAND_ALIASES`, asserted by a parametrised unit test |
| Fun gate | **identical** — `deny_if_disabled(message, ctx, "fun")`, one `fun_off` reply, nothing else sent |
| Target resolution, branch ① (tagged) | **identical** — the raw second whitespace token, no membership lookup |
| Target resolution, branch ② (reply) | **identical** — replied-to sender's `first_name`, never their username |
| Target resolution, branch ③ (caller) | **identical, including D-DE-1** — `@username` when present, bare `first_name` with the skull prefix dropped when absent |
| Caption template/reason | **identical** — `locales.get_nested("death", "template"/"Reason", lang, ...)` over the ported `lib.json` `death` object and `locales.lines("death", lang)` over the ported 81-line `death.txt`, same `%`-placeholder fallback `locales.get` gives every flat key |
| React + chat action order (D-DE-2) | **identical** — both fire before the pool is even picked, so an empty pool looks like v1's own pre-crash state |
| Asset pool source | **changed, necessarily** — `legacy_assets.choose("Death", rng)` over the bucket export instead of a live GCS listing; picking is otherwise the same "one random blob" v1 does |
| gif vs. photo dispatch | **identical outcome, different field read** — `source_path.lower().endswith(".gif")` instead of v1's case-sensitive `filename.endswith('.gif')`; see "The gif/photo decision" above |
| Empty pool (D-DE-3) | **fixed** — `None` and a log line instead of an uncaught `ValueError` |
| Failure mid-sequence | **identical** — no try/except around the chat-action/storage-fetch/send calls beyond the reaction's own; an exception propagates |
| Persistence | **identical** — none |
| Random source | **identical distribution** — a plain `random.Random` instance (`_rng`), matching `ship.py`'s/`complaint.py`'s idiom |

## Tests

| Layer | File |
|---|---|
| Unit — alias resolution, `resolve_target`'s three branches + D-DE-1, `render_caption` across `en`/`pt`/`es`, `is_gif`, the empty-pool degrade (D-DE-3) and the gif/photo dispatch via `_deliver` | `packages/cb-gateway/tests/test_death.py` |
| Integration — none; the feature writes no row (Persistence: none), same reasoning `fun_complaint`'s contract gives for skipping one | n/a |
| Acceptance — the two copied QA scenarios plus the fun-off gate, every trigger spelling, the reply-based target, the still-image dispatch and the empty-pool degrade | `qa/features/fun_death.feature`, `qa/test_fun_death.py` |
