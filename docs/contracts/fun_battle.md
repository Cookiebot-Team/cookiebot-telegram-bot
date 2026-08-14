# Contract: fun_battle (v1 -> v2)

Phase 2/6 of `/migrate-feature` for `/battle`, `/batalha`, `/batalla`. QA:
`../Cookiebot-QA/features/fun_battle.feature`. FEATURE-MAP row: `fun_battle`.
Spec/design: `.specs/features/fun_battle/{spec,design,tasks}.md` — read those
for the full reasoning; this file is the durable behaviour record. Files
owned by this port: `packages/cb-gateway/src/cb_gateway/handlers/battle.py`
(new), `packages/cb-gateway/src/cb_gateway/handlers/__init__.py` (one router
line), `packages/cb-gateway/tests/test_battle.py` (new), `qa/mock_telegram.py`
(`getUserProfilePhotos`, `sendMediaGroup`'s list-response shape, `sendPoll`
added to the generic Message-response set), `qa/features/fun_battle.feature`
(new), `qa/test_fun_battle.py` (new), this file.

## All three of v1's shapes ship

v1's `battle` (`../COOKIEBOT-Telegram-Group-Bot/Bot/SocialContent.py:294-379`)
hides three completely different code paths behind one trigger — two people,
one tagged person vs. a random "fighter" character, or the caller vs. a
random fighter. The **two-people** shape shipped first; the two fighter
shapes were `Status.PARTIAL` while v1's private GCS bucket was unreachable
and answered `battle_no_picture` in the meantime. That bucket has since been
exported (`cb_worker.bucket_export`) and catalogued (`cb.py legacy-catalog`),
so both now do what v1 does — see "The fighter shapes" below.

## Phase 1 — where v1 lives

- Handler: `battle`, `SocialContent.py:294-379`.
- Dispatch: `COOKIEBOT.py:216,218-219,224-225` — inside the shared fun `elif`
  chain, gated on `functionsFun` (`fun_off` when disabled).
- Target parsing: `get_members_tagged`, `SocialContent.py:104-111`.
- Locale strings: `battle_no`, `battle_extract`, `battle_title`, `battle_type`,
  `battle_rule`, `battle_equip`, `battle_full`, `battle_private`,
  `battle_no_picture` (plus PT-only `battle_title_plus`/`battle_title_list`,
  unused by this slice) — all already in `cb_core/locale_data/`, byte-identical
  to v1.

## Phase 2 — v1 behaviour contract

| Aspect | v1 behaviour (file:line) |
|---|---|
| Triggers | `/battle`, `/batalha`, `/batalla` (aliased in `cb_core/textmatch.py`, out of this port's ownership, already done) |
| Preconditions | `functionsFun` gate shared with `fun_ship`/`fun_death`/`fun_complaint` (`COOKIEBOT.py:218-219`) |
| Cooldowns / quotas | None |
| Shape selection | `len(tags) > 1 or "random" in text.lower()` ⇒ two-people; `"random"` wins over explicit tags when both are present (v1 re-checks it once shape is already decided, `:298-299`); else `len(tags) == 1` ⇒ one-tag; else ⇒ self. Only the first two tags are ever used; a third+ is silently ignored. |
| Two-people success | `sendMediaGroup` (two photos, first captioned `"{a} VS {b}"` or `"@{a} VS @{b}"` for `"random"` picks + flavour suffix), then `sendPoll(is_anonymous=False, allows_multiple_answers=False)`, both as replies to the trigger — v1's exact shape (`:328-343`) |
| Two-people failure | fewer than two eligible `"random"` candidates ⇒ `battle_no`; either side's photo extraction fails ⇒ `battle_extract` naming that side, checked in order (first side before the second is even attempted) |
| One-tag success | that person's photo vs. a random `Fight/` fighter, coin-flipped order, `sendMediaGroup` + `sendPoll` as replies (`:346-357,366-379`). Caption is a bare `"{a} VS {b}"` — **no flavour suffix**, unlike the two-people shape |
| One-tag failure | the tagged user's photo could not be fetched ⇒ `battle_private` — a *different* string from the two-people shape's `battle_extract` (`:352`) |
| Self success | the caller's own photo vs. a random `Fight/` fighter, same tail. The caller is named `username` or, with none, `first_name` — **without** an `@` (`:359`) |
| Self failure | `getUserProfilePhotos` returns nothing (v1: `IndexError`) ⇒ `battle_no_picture` (`:361-364`) |
| Fighter pool | `pt` draws from *either* pool (`random.choice(random.choice([eng, pt]))`, `:367`) so both are equally likely regardless of size (711 English, 114 Portuguese); every other language draws English only (`:370`). The fighter's name comes from its filename, not a locale string: `.split('/')[-1]`, `.png`/`.jpg`/`.jpeg` stripped, `_` → space, `.capitalize()` (`:373`) |
| Poll title (fighter shapes) | `pt` gets `battle_title_plus` with a random `battle_title_list` suffix; every other language gets the plain `battle_title` (`:368,371`) |
| What closes the poll | Nothing. No `stopPoll` anywhere in v1 — a `/battle` poll stays open until a human closes it by hand. Vote tallies live entirely inside Telegram (a genuine native poll, `is_anonymous=False`), never in v1's process or a backend table — **no vote-state defect of the `core_stickerspam`-in-process-counter shape exists here**, because the state was never in v1's process to begin with. |
| Persistence | None — no table, no row |
| External calls (v1) | `telegram.me` HTML scrape (BeautifulSoup) for two-people/one-tag photos, GCS signed-URL read for the fighter image, Bot API `getUserProfilePhotos` for the self case only |

## Defects — verdict per item

| id | Defect | Verdict |
|---|---|---|
| D-BT-1 | Race condition: all three shapes wrote fetched photos to hardcoded, non-namespaced local filenames (`user1.jpg`/`user2.jpg`/`user.jpg`) — two concurrent `/battle` calls could overwrite each other's temp file mid-flight. | **fixed** — the redesign (below) never touches local disk; there is no file to race over |
| D-BT-2 | Fragile, undocumented dependency: two-people/one-tag photos came from unauthenticated HTML scraping of `telegram.me`, a page structure Telegram does not version, requiring a public username and a publicly visible profile photo. | **fixed the mechanism, preserved the outcome** (accepted decision) — same class of fix `util_embedder`'s contract already established for v1's synchronous link-validation defect |
| D-BT-3 | Crash: `battle_extract`'s `%(user)s` substitution read `members_tagged[0]`/`[1]` unconditionally, regardless of which sub-path was live — an `IndexError` on e.g. bare `/battle random` (zero `@` tags) whenever extraction failed, propagating to the dispatcher's bare `except` and silently dropping the update. Found while writing this port's spec, not previously documented. | **fixed** — this port always names whichever side actually failed by construction; there is no index into a list that may not apply |

## The accepted redesign — what actually fetches a photo now

`cb_core.members.roster(group_id)` (the same registry `fun_ship`/
`util_everyone` already read) resolves a tagged username, or a `"random"`
pick, to a real Telegram `user_id` — case-insensitively, matching the
outcome v1's scrape got for free from `telegram.me`'s own case-insensitive
routing. `bot.get_user_profile_photos(user_id, limit=1)` — the exact Bot API
call v1's own self-battle path already used — resolves that id to a
`file_id`, handed straight into a new `InputMediaPhoto`. No download, no
OpenCV round-trip, no temp file.

**Accepted behavioural drift**: a tagged user who has never spoken in this
group cannot be resolved this way, where v1's scrape could sometimes reach
such a user via their public `telegram.me` web-preview page. This is **not a
new failure mode** — an unresolvable tag falls into v1's own `battle_extract`
message, the same string a failed scrape would already have produced, naming
the same tagged text. A user only notices *that* the battle failed, never a
difference in *how*.

## The fighter shapes

`bloblist_fighters_eng`/`bloblist_fighters_pt` (`SocialContent.py:24-25`) were
`storage_bucket.list_blobs(prefix="Fight/English"|"Fight/Portuguese")` against
v1's private `cookiebot-bucket` — the same bucket `fun_death`'s `Death/` prefix
reads from. Both prefixes are exported now (711 + 114 objects), and this port
reads them the way `death.py` reads its own pool: `legacy_assets.choose` for the
catalog row, `cb_core.storage` for the bytes, a `BufferedInputFile` into the
media group. v1's 15-minute signed URL has no equivalent and needs none — the
bytes are ours now.

Only the *human* half changed mechanism: the one-tag shape resolves its tag
through the roster and `get_user_profile_photos` rather than scraping
`telegram.me`, exactly as the two-people shape already does, and both the
unresolvable-tag case and the no-visible-photo case answer v1's own
`battle_private`. The self shape's photo lookup is byte-for-byte v1's, because
v1 already used the Bot API there.

**An un-catalogued pool sends nothing.** `legacy_assets.choose` returns `None`
in a deployment where `cb.py legacy-catalog` has never run; the handler logs
`battle.fighter_pool_empty` and stops, after the reaction and chat action have
already gone out. v1 had no equivalent — an empty bucket listing crashed in
`random.choice(...)`. Same decision, same reasoning as `fun_death`'s D-DE-3.

## Phase 6 — parity table

| Aspect | Verdict |
|---|---|
| Triggers (`/battle`, `/batalha`, `/batalla`) | **same** |
| `functionsFun` gate | **same** |
| Shape selection, including `"random"` winning over explicit tags | **same, byte-for-byte** |
| Target parsing (`get_members_tagged`'s raw-substring/trailing-text quirk, case-sensitive `.endswith('bot')`) | **same, warts included** |
| Photo source for two-people/one-tag | **changed (intentional, accepted)** — roster + `get_user_profile_photos` replaces the `telegram.me` scrape (D-BT-2) |
| Unresolvable tag | **changed (accepted drift)** — falls into v1's existing `battle_extract`, not a new failure; a narrower success case than v1's occasionally-successful scrape |
| Caption `@`-prefix inconsistency between explicit-tag and `"random"` shapes | **same, preserved** — not normalised |
| Poll shape (`is_anonymous=False`, `allows_multiple_answers=False`, native Telegram poll) | **same** |
| What closes the poll | **same** — nothing, in both v1 and v2 |
| Local temp-file race (D-BT-1) | **fixed** — no local file exists to race over |
| `battle_extract` naming bug on `"random"` extraction failure (D-BT-3) | **fixed** — no equivalent code path exists |
| One-tag / self shapes | **same** — both ship, reading the exported `Fight/English` and `Fight/Portuguese` pools |
| Fighter name derivation (`.capitalize()` lower-casing the rest, only three extensions stripped) | **same, warts included** — a `.gif` fighter still carries its extension in the poll option |
| `pt` drawing from either fighter pool, and its `battle_title_plus` poll title | **same** |
| Fighter image transport | **changed (mechanism only)** — bytes from `cb_core.storage` instead of a 15-minute GCS signed URL; the user sees the same photo |
| Empty fighter pool | **changed (fixed)** — logs and sends nothing, where v1 raised `ValueError` inside `random.choice` |

## QA

`../Cookiebot-QA/features/fun_battle.feature` has one scenario, and it targets
v1's **one-tag** path ("tags another user", singular). It was skipped while
that shape was blocked and runs for real now; its wording is unchanged, and
its "Option A"/"Option B" phrasing still does not name the real poll options
(the two display names) — recorded as a QA-vs-v1 conflict rather than
reconciled, per AGENTS.md §1. Nine net-new scenarios cover the rest: two
explicit tags, `"random"` (enough and too-few members), an unresolvable tag,
the caller-vs-fighter shape, a caller with no photo, a tagged member with no
visible photo, an un-catalogued fighter pool, and the `fun_off` gate.

## Tests

| Layer | File |
|---|---|
| Unit — target parsing, shape selection, roster resolution, catalog reads, caption assembly | `packages/cb-gateway/tests/test_battle.py` |
| Acceptance — QA's own one-tag scenario + nine net-new | `qa/features/fun_battle.feature`, `qa/test_fun_battle.py` |

No integration-layer test: this feature has no persistence and no Citus-hot
query of its own (`members.roster`'s own single-shard plan is already
asserted by `qa/integration/test_everyone.py`).
