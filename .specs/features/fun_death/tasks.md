# fun_death — Tasks

**Update — T0 landed.** `cb_worker.bucket_export` + `cb.py legacy-catalog`
replaced the vendoring plan this file originally described: the `Death/`
prefix is not copied byte-identical into
`packages/cb-core/src/cb_core/asset_data/death/` (too large — 21.5 MB across
34 files, `spec.md`'s "Update" paragraph and `design.md`'s R2 correction).
Instead the bytes live in `cb_core.storage` under content-addressed keys and
a small per-prefix catalog ships as package data, read through
`cb_core.legacy_assets.choose("Death", rng)`. T1-T5 below were executed
against that corrected shape, not the original vendoring plan — see
`docs/contracts/fun_death.md` for what actually shipped.

## Status

| Task | Status | Notes |
|------|--------|-------|
| T0 — Unblock the asset pool | ✅ done | not vendoring (see the update note above) — `cb_worker.bucket_export` + `cb.py legacy-catalog` land the `Death/` prefix in `cb_core.storage` behind `cb_core.legacy_assets` instead |
| T1 — Handler: gate, target resolution, caption | ✅ done | `packages/cb-gateway/src/cb_gateway/handlers/death.py` |
| T2 [P] — Nested-catalog reader | ✅ done, superseded | `locales.get_nested`/`nested_value` landed in `cb_core/locales.py` since this task was written and already solve R1 generically — no local `_death_strings` needed, unlike `groupguardian.py`'s `_captcha_strings` precedent this task originally pointed at |
| T3 — Router registration | ✅ done | `packages/cb-gateway/src/cb_gateway/handlers/__init__.py`, next to `complaint`/`dice`/`ship` |
| T4 [P] — Unit tests | ✅ done | `packages/cb-gateway/tests/test_death.py` — trigger resolution, target resolution (all three branches + D-DE-1), caption rendering in `en`/`pt`/`es`, `is_gif`, and the empty-pool degrade (D-DE-3) via the `_deliver` seam |
| T5 — Acceptance: `qa/features/fun_death.feature`, `qa/test_fun_death.py` | ✅ done | the two upstream scenarios copied verbatim, plus the fun-off gate, every trigger spelling, the reply-based target, the still-image dispatch and the empty-pool degrade |
| T-final — Close out | ✅ done | `docs/contracts/fun_death.md`, `scripts/spec.py` flipped to `Status.DONE`, `docs-sync` run, this file's status table updated |

## T0 — Vendor the `Death/` asset export

- **Skills:** none (infrastructure action, not a code change)
- **What:** Export every object under `gs://cookiebot-bucket/Death/` (or
  whatever the real bucket/prefix combination turns out to be — `spec.md`
  names the prefix as read from `Bot/Miscellaneous.py:17`) and place the
  files under `packages/cb-core/src/cb_core/asset_data/death/`, byte-identical,
  same discipline `fun_complaint`'s `T1` used for `Bot/Static/reclamacao/`.
- **Where:** `packages/cb-core/src/cb_core/asset_data/death/` (new)
- **Depends on:** none — this is the prerequisite
- **Done when:** the directory exists, is non-empty, and every file's
  extension is recorded back into `design.md`'s open decision #1 (replacing
  the placeholder `.jpg`/`.png`/`.gif` guess with what actually shipped).
- **Gate:** none — nothing to lint or test until files exist
- **Commit:** `chore(fun_death): vendor v1's Death asset pool`

## T1 — Handler: gate, target resolution, caption

- **Skills:** /migrate-feature
- **What:** New `packages/cb-gateway/src/cb_gateway/handlers/death.py`
  implementing design R2 (pool read, gif/photo branch), R3 (target
  resolution branches ①②③, caption assembly, D-DE-1 preserved verbatim),
  R4 (telemetry). `ctx.enabled("fun")` gate mirrors `ship.py`/`firecracker.py`
  exactly.
- **Where:** `packages/cb-gateway/src/cb_gateway/handlers/death.py` (new)
- **Depends on:** T0
- **Reuses:** `cb_core.assets.pool`/`path`, `cb_gateway.context.context_for`/`t`,
  `ParsedCommand.args` (`ship.py:104-116`'s `explicit_targets` is the template
  for reading it)
- **Done when:** the handler compiles and the pure caption-assembly function
  is unit-testable without Telegram or a database.
- **Gate:** `uv run mypy packages/cb-gateway/src/cb_gateway/handlers/death.py`
- **Commit:** `feat(fun_death): port /death's target resolution and caption`
- **→ R2, R3 (design.md)**

## T2 [P] — Nested-catalog reader (`_death_strings`)

- **Skills:** /migrate-feature
- **What:** Local `_death_strings(lang) -> Mapping[str, object]` in
  `death.py`, copying `groupguardian.py:108-125`'s `_captcha_strings` pattern
  exactly (design R1) — cast `locales.catalog(lang)`, reach into `"death"`,
  fall back to `en` by hand, replicate `locales.get`'s malformed-placeholder
  try/except for the two `%`-substitutions this feature needs.
- **Where:** `packages/cb-gateway/src/cb_gateway/handlers/death.py`
- **Depends on:** T0
- **Reuses:** `groupguardian.py`'s `_captcha_strings` as the literal template
- **Done when:** `_death_strings("en")["template"] % {"variant": ...}` and
  `_death_strings("en")["Reason"] % {"line": ...}` both round-trip against
  the real ported `lib.json` values.
- **Gate:** `uv run mypy packages/cb-gateway/src/cb_gateway/handlers/death.py`
- **Commit:** folded into T1's commit
- **→ R1 (design.md)**

## T3 — Router registration

- **Skills:** /migrate-feature
- **What:** One line in `packages/cb-gateway/src/cb_gateway/handlers/__init__.py`'s
  disjoint-trigger command block (next to `ship`/`firecracker`/`everyone`) —
  order-independent, no join-chain interaction.
- **Where:** `packages/cb-gateway/src/cb_gateway/handlers/__init__.py`
- **Depends on:** T1
- **Done when:** `/death` reaches the handler in a real dispatcher.
- **Gate:** `uv run mypy packages/cb-gateway/src/cb_gateway/handlers/__init__.py`
- **Commit:** folded into T1's commit
- **→ design.md module-placement table**

## T4 [P] — Unit tests

- **Skills:** /migrate-feature
- **What:** `packages/cb-gateway/tests/test_death.py` — target-resolution
  branches ①②③ including D-DE-1's dropped-emoji case, caption assembly
  against the real `en`/`pt`/`es` catalog values, `.gif` vs. photo dispatch,
  the `fun_off` gate.
- **Where:** `packages/cb-gateway/tests/test_death.py` (new)
- **Depends on:** T1, T2
- **Gate:** `uv run pytest packages/cb-gateway/tests/test_death.py -q`
- **Commit:** folded into T1's commit
- **→ R3, R4 (design.md)**

## T5 — Acceptance: `qa/features/fun_death.feature`, `qa/test_fun_death.py`

- **Skills:** /migrate-feature
- **What:** Copy `../Cookiebot-QA/features/fun_death.feature` verbatim
  (spec.md's QA section: no wording conflict to reconcile). Step
  definitions drive the real dispatcher against the mock Telegram API, same
  pattern every other acceptance suite in `qa/` uses.
- **Where:** `qa/features/fun_death.feature` (new), `qa/test_fun_death.py` (new)
- **Depends on:** T1, T3
- **Gate:** `uv run pytest qa/test_fun_death.py -q`
- **Commit:** `test(fun_death): acceptance scenarios`
- **→ QA section (spec.md)**

## T-final — Close out

- **Skills:** none
- **What:** `docs/contracts/fun_death.md` (Phase 2/6 tables), flip
  `scripts/spec.py`'s `status` to `Status.DONE`, `cb.py docs-sync`, real
  prose in `docs/site/content/docs/features/fun_death.mdx`, flip this file's
  Status table to `✅ done`.
- **Where:** as listed above
- **Depends on:** T1-T5
- **Done when:** `uv run python scripts/cb.py check` exits 0.
- **Gate:** `uv run python scripts/cb.py check`
- **Commit:** `docs(fun_death): close out`
