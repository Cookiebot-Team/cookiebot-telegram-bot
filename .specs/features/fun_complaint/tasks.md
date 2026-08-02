# fun_complaint — Tasks

Grammar: `tlc-spec-driven` §5. Read `spec.md` and `design.md` first.

## Status

| Task | Status | Notes |
|------|--------|-------|
| T1 — Static assets and the `cb_core.assets` accessor | ✅ done | unblocks everything; `fun_death`/`fun_meme` reuse it |
| T2 — Failing unit tests for both entry points | ✅ done | depends on T1 for `assets.pool` |
| T3 — Handler: the Milton prompt and the hold | ✅ done | turns T2 green |
| T4 — Acceptance scenarios | ✅ done | delay monkeypatched to 0 |
| T-final — Close out | ✅ done | contract, feature-map conflicts, spec.py flip |

## Tasks

### T1 — Static assets and the `cb_core.assets` accessor

- **Skills:** /implement-feature (this piece is net-new plumbing, not a port)
- **What:** Copy the ten files in
  `../COOKIEBOT-Telegram-Group-Bot/Bot/Static/reclamacao/` into
  `packages/cb-core/src/cb_core/asset_data/complaint/` byte-identical
  (`milton_pt.jpg`, `milton_eng.jpg`, `hold{1,2,3,4,5,6,7,9}.wav` — there is no
  `hold8.wav`, do not renumber). Add `cb_core/assets.py` with `path(*parts)` and
  `pool(*parts, suffix)` per design R1.2, `pool` returning a **sorted** tuple.
  Extend the existing package-data declaration in `pyproject.toml` that ships
  `locale_data` so it ships `asset_data` too — one mechanism, not two.
- **Where:** `packages/cb-core/src/cb_core/asset_data/complaint/*` (new),
  `packages/cb-core/src/cb_core/assets.py` (new), `pyproject.toml`,
  `packages/cb-core/tests/test_assets.py` (new).
- **Depends on:** none
- **Reuses:** whatever `cb_core/locales.py` does to read `locale_data` from an
  installed package — same `importlib.resources` idiom, same packaging entry.
- **Done when:** `diff -r` against the v1 directory is clean, `pool("complaint",
  suffix=".wav")` returns exactly 8 sorted paths, and the byte-identity test
  skips cleanly when the v1 checkout is absent (design R5.2).
- **Gate:** `uv run pytest packages/cb-core/tests/test_assets.py -q`
- **Commit:** `feat(cb-core): ship v1's static assets and one way to reach them`
- **→ R1**

### T2 — Failing unit tests for both entry points

- **Skills:** /migrate-feature (Phase 4)
- **What:** Write the unit tests before the handler exists, per design R5.1:
  every command alias (`/milton /reclamacao /reclamação /complaint /queja`,
  bare, with argument, with `@botname`) resolving to canonical `complaint`;
  `_is_milton_reply` accepting a caption that *contains* `Milton do RH.` or
  `Milton from HR.` anywhere, rejecting a caption with neither, rejecting a
  `reply_to_message` that has `text` but no `caption`, and rejecting a message
  that is itself a command; the protocol string matching `^\d{2}-\d{6}/\d{4}$`
  over a seeded `random.Random`; photo choice `pt → milton_pt.jpg`, everything
  else → `milton_eng.jpg`.
- **Where:** `packages/cb-gateway/tests/test_complaint.py` (new). If
  `cb_core/textmatch.py:COMMAND_ALIASES` is missing any of the five spellings,
  add them **alongside** the existing entries in the same commit.
- **Depends on:** T1
- **Reuses:** `packages/cb-gateway/tests/test_rules.py` for how the existing
  two-step reply predicate is unit tested.
- **Done when:** alias tests pass and the predicate/protocol tests fail on
  import because `cb_gateway.handlers.complaint` does not exist.
- **Gate:** `uv run pytest packages/cb-gateway/tests/test_complaint.py -q`
  (expected: alias tests pass, the rest error on import)
- **Commit:** `test(fun_complaint): the reply predicate and every v1 trigger`
- **→ R2.3, R2.4, R5.1**

### T3 — Handler: the Milton prompt and the hold

- **Skills:** /migrate-feature (Phase 5)
- **What:** Implement both entry points in one router per design R2–R4.
  Entry 1: fun gate → `sendChatAction upload_photo` → reply with
  `milton_pt.jpg` (lang `pt`) or `milton_eng.jpg` (everything else), caption
  `t(ctx, "complaint", user=<sender first_name>)`.
  Entry 2: fun gate → delete the replied-to photo (suppressed) →
  `sendChatAction upload_audio` → send a random hold `.wav` as a **voice** note
  replying to the user, caption `Protocol: NN-NNNNNN/YYYY` → schedule the tail
  with `asyncio.create_task` (delay `rng.randint(10, 20)`, injectable per R3.4;
  keep a module-level task set per R3.2) which deletes the voice note and
  replies with `rng.choice(locales.lines("answers", ctx.lang))`. Comment the
  restart caveat and point at HANDOFF §1 gap 5.
- **Where:** `packages/cb-gateway/src/cb_gateway/handlers/complaint.py` (new),
  `packages/cb-gateway/src/cb_gateway/handlers/__init__.py` (one import + one
  `include_router`, disjoint-commands block).
- **Depends on:** T2
- **Reuses:** `handlers/ship.py` (fun gate, `mark_outcome`, `rng`,
  `locales.lines` choice), `handlers/rules.py` (`_is_*_reply` predicate shape,
  suppressed deletes), `handlers/groupguardian.py:504-507` (the
  `asyncio.create_task` + task-set idiom, verbatim), `cb_core/assets.py` from T1.
- **Done when:** T2 is fully green and the router is registered exactly once.
- **Gate:** `uv run pytest packages/cb-gateway/tests/test_complaint.py -q && uv run python scripts/cb.py types`
- **Commit:** `feat(fun_complaint): Milton from HR, the hold music and the verdict`
- **→ R2, R3, R4**

### T4 — Acceptance scenarios

- **Skills:** /migrate-feature (Phase 3 + top of Phase 4)
- **What:** Copy `../Cookiebot-QA/features/fun_complaint.feature` into
  `qa/features/` **wording unchanged** (both scenarios, trailing whitespace
  included), then append two net-new scenarios: fun functions disabled ⇒ one
  `fun_off` reply and nothing else; a reply to a photo whose caption lacks both
  signatures ⇒ the bot says nothing. Drive them against mock Telegram: assert one
  photo send with the caption, then on reply assert the prompt was deleted, a
  voice note went out with a caption matching `^Protocol: \d{2}-\d{6}/\d{4}$`,
  and — after the monkeypatched zero delay and awaiting the scheduled task — the
  voice note was deleted and one of the `answers` lines was sent.
- **Where:** `qa/features/fun_complaint.feature` (new),
  `qa/test_fun_complaint.py` (new).
- **Depends on:** T3
- **Reuses:** `qa/test_core_rules.py` (two-step reply flow against mock
  Telegram), `qa/conftest.py`, `next_update_id()` for every update.
- **Done when:** all four scenarios pass, no real sleeping, and no existing
  acceptance test regressed.
- **Gate:** `uv run pytest qa/test_fun_complaint.py -q`
- **Commit:** `test(fun_complaint): the QA scenarios, the gate and the near miss`
- **→ R5.4**

### T-final — Close out

- **Skills:** none
- **What:** Write `docs/contracts/fun_complaint.md` — Phase-2 table with v1
  `file:line` intact, Phase-6 parity table, the five D-CP verdicts, why the
  assets are package data rather than `cb_core.storage` (R1.3), and the
  in-process-tail restart caveat (R3.3). Record the three QA/v1 conflicts from
  `spec.md` §QA in `docs/site/content/docs/feature-map.mdx`. Flip
  `fun_complaint` to `done` in `scripts/spec.py`, correcting its `triggers` list
  to all five spellings there, then `cb.py docs-sync`. Mark the rows done in this
  file's Status table.
- **Where:** `docs/contracts/fun_complaint.md` (new),
  `docs/site/content/docs/feature-map.mdx`, `scripts/spec.py`, regenerated
  `docs/site/**`, `.specs/features/fun_complaint/tasks.md`.
- **Depends on:** T4
- **Reuses:** `docs/contracts/core_rules.md` (two-step flow contract format).
- **Done when:** `cb.py check` exits 0.
- **Gate:** `uv run python scripts/cb.py check`
- **Commit:** `docs(fun_complaint): close out`
- **→ R6**
