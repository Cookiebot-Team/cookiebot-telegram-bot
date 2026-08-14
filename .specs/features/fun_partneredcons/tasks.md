# fun_partneredcons — Tasks

Grammar: `tlc-spec-driven` §5. Read `spec.md` (the v1 contract and the asset
investigation) and `design.md` (R1-R5, the three answered `/trex` questions)
first.

## Status

| Task | Status | Notes |
|------|--------|-------|
| T1 — `number_to_emojis` in cb-core | ✅ done | inverse of the `emojis_to_numbers` already there |
| T2 — Handler, event table and router registration | ✅ done | R1-R5 |
| T3 — Acceptance scenarios | ✅ done | QA's six as an Outline + four net-new |
| T-final — Close out | ✅ done | spec row BLOCKED → DONE |

## Tasks

### T1 — `number_to_emojis` in cb-core

- **Skills:** /migrate-feature
- **What:** Port `universal_funcs.py:346-351` next to the keycap table
  `emojis_to_numbers` already reads, deriving the reverse mapping rather than
  writing the ten pairs out twice.
- **Where:** `packages/cb-core/src/cb_core/publisher.py`,
  `packages/cb-core/tests/test_publisher.py`
- **Depends on:** none
- **Reuses:** `_KEYCAP_DIGITS`
- **Done when:** `number_to_emojis(121) == "1️⃣2️⃣1️⃣"`.
- **Gate:** `uv run pytest packages/cb-core/tests/test_publisher.py -q`
- **Commit:** folded into T2's commit
- **→ R2.3**

### T2 — Handler, event table and router registration

- **Skills:** /migrate-feature
- **What:** The event table (R1), the countdown maths (R2), the `cta` lookup
  (R3), six stacked command filters with no feature gate (R4) and the
  empty-pool degradation (R5). Unit tests for every pure function, including
  the wraparound and the happening-now window.
- **Where:** `packages/cb-gateway/src/cb_gateway/handlers/partneredcons.py`,
  `packages/cb-gateway/src/cb_gateway/handlers/__init__.py`,
  `packages/cb-gateway/tests/test_partneredcons.py`
- **Depends on:** T1, the `legacy-catalog` generation commit
- **Reuses:** `death.py`'s pool-read shape, `owner.py`'s stacked-filter idiom
- **Done when:** each of the six commands replies with a photo, five of them
  captioned with a countdown.
- **Gate:** `uv run pytest packages/cb-gateway/tests/test_partneredcons.py -q`
- **Commit:** `feat(fun_partneredcons): six convention posters, dates and all`
- **→ R1.1-R5.1**

### T3 — Acceptance scenarios

- **Skills:** /migrate-feature (Phase 5)
- **What:** Sync QA's six scenarios as a `Scenario Outline` (dropping its
  duplicated `/fursmeet`), add four covering the countdown caption, `/trex`'s
  caption-less send, the ungated dispatch and the empty pool.
- **Where:** `qa/features/fun_partneredcons.feature`,
  `qa/test_fun_partneredcons.py`
- **Depends on:** T2
- **Reuses:** `qa/test_fun_death.py`'s `legacy_assets.choose` + `memory://`
  storage seam
- **Done when:** all ten scenarios pass.
- **Gate:** `uv run pytest qa/test_fun_partneredcons.py -q`
- **Commit:** folded into T2's commit
- **→ R4.2, R5.1**

### T-final — Close out

- **Skills:** none
- **What:** `docs/contracts/fun_partneredcons.md` with the Phase-2 and Phase-6
  tables and the three answered `/trex` questions; flip `scripts/spec.py` to
  `Status.DONE`; `cb.py docs-sync`; prose on the feature page; QA's duplicated
  scenario recorded in `feature-map.mdx`; `HANDOFF.md`.
- **Where:** `docs/contracts/fun_partneredcons.md`, `scripts/spec.py`,
  `docs/site/content/docs/features/fun_partneredcons.mdx`,
  `docs/site/content/docs/feature-map.mdx`, `HANDOFF.md`, this file
- **Depends on:** T3
- **Done when:** `uv run python scripts/cb.py check` exits 0.
- **Gate:** `uv run python scripts/cb.py check`
- **Commit:** folded into T2's commit
- **→ R1-R5**
