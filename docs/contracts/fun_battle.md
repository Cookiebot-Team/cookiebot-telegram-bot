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

## This slice ships one of v1's three shapes

v1's `battle` (`../COOKIEBOT-Telegram-Group-Bot/Bot/SocialContent.py:294-379`)
hides three completely different code paths behind one trigger — two people,
one tagged person vs. a random "fighter" character, or the caller vs. a
random fighter. Only the **two-people** shape ships in this slice
(`Status.PARTIAL` in `scripts/spec.py`). The other two need a private GCS
bucket export this environment cannot reach — see "What's still blocked"
below, and `docs/contracts/fun_death.md` / `.specs/features/fun_death/spec.md`
for the identical prerequisite already documented there.

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
| One-tag / self | pit that person (or the caller) against a random "fighter" image from a private GCS bucket — see "What's still blocked" |
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

## What's still blocked — one-tag and self shapes

Both need a "fighter" opponent image from `bloblist_fighters_eng`/
`bloblist_fighters_pt` (`SocialContent.py:24-25`,
`storage_bucket.list_blobs(prefix="Fight/English"|"Fight/Portuguese")`) — the
same private `cookiebot-bucket` GCS bucket `fun_death`'s `bloblist_death`
reads from, a different prefix. Confirmed (same evidence
`fun_death`'s contract already gives for its own prefix): no `Fight`/
`fighter`-named asset anywhere in the `../COOKIEBOT-Telegram-Group-Bot`
checkout, no credential anywhere in this environment.

**Temporary route, not final behaviour**: rather than inventing a new
"not implemented yet" string, both shapes reply `battle_no_picture` —
v1's own, already-ported "you need a profile picture (or it's private)"
string, repurposed for "there is no fighter image to use" (also literally
true). No roster lookup, no Bot API call, no photo resolution is attempted
for these two shapes at all — the branch is detected and answered
immediately (`cb_gateway/handlers/battle.py`'s `battle` function, the
`shape is not BattleShape.TWO_PEOPLE` branch). This is replaced once the
`Fight/` prefix is exported and vendored into `cb_core/asset_data/fight/`
(mirroring `fun_death`'s still-blocked plan for its own pool) — tracked as
the same `HANDOFF.md` gap as `fun_death`, not a separate one.

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
| One-tag / self shapes | **not yet ported** — temporary `battle_no_picture` reuse, blocked on the `Fight/` GCS export, same prerequisite as `fun_death` |

## QA

`../Cookiebot-QA/features/fun_battle.feature` has one scenario, and it targets
v1's **one-tag** path ("tags another user", singular) — the shape this slice
does not ship. `qa/features/fun_battle.feature` copies it wording-unchanged;
its step definition (`qa/test_fun_battle.py::qa_scenario_not_yet_reachable`)
calls `pytest.skip()` with the same reason rather than asserting an outcome
that doesn't exist yet or silently repurposing the wording to check something
else. Six net-new scenarios cover what this slice actually ships: two explicit
tags, `"random"` (enough and too-few members), an unresolvable tag, the bare
`/battle` temporary route, and the `fun_off` gate.

## Tests

| Layer | File |
|---|---|
| Unit — target parsing, shape selection, roster resolution, catalog reads, caption assembly | `packages/cb-gateway/tests/test_battle.py` |
| Acceptance — one skipped (QA's one-tag scenario) + six net-new | `qa/features/fun_battle.feature`, `qa/test_fun_battle.py` |

No integration-layer test: this feature has no persistence and no Citus-hot
query of its own (`members.roster`'s own single-shard plan is already
asserted by `qa/integration/test_everyone.py`).
