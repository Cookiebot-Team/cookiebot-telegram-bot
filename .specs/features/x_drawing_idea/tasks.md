# x_drawing_idea — Tasks

Grammar: `tlc-spec-driven` §5.

## Status

| Task | Status | Notes |
|------|--------|-------|
| T1 — Handler, aliases and router registration | ✅ done | R1, R2 |
| T2 — Acceptance scenarios | ✅ done | authored locally; no upstream QA |
| T-final — Close out | ✅ done | spec row PLANNED → DONE |

## Tasks

### T1 — Handler, aliases and router registration

- **Skills:** /migrate-feature
- **What:** `pick_reference` (R1) and the handler (R2), plus the three
  aliases, plus unit tests covering the inclusive bounds, reproducibility and
  the empty pool — and one asserting the shipped catalog is sorted, since the
  caption's id is a position in it.
- **Where:** `packages/cb-gateway/src/cb_gateway/handlers/drawing_idea.py`,
  `packages/cb-core/src/cb_core/textmatch.py`,
  `packages/cb-gateway/src/cb_gateway/handlers/__init__.py`,
  `packages/cb-gateway/tests/test_drawing_idea.py`
- **Depends on:** the `legacy-catalog` generation commit
- **Reuses:** `death.py`'s pool/storage shape
- **Done when:** `/drawingidea` replies with a captioned photo.
- **Gate:** `uv run pytest packages/cb-gateway/tests/test_drawing_idea.py -q`
- **Commit:** `feat(x_drawing_idea): 3,435 references, and the id in the caption`
- **→ R1.1-R2.2**

### T2 — Acceptance scenarios

- **Skills:** /implement-feature (Phase 5 — no QA scenario to port)
- **What:** Three triggers, the utility gate, the empty pool.
- **Where:** `qa/features/x_drawing_idea.feature`, `qa/test_x_drawing_idea.py`
- **Depends on:** T1
- **Reuses:** `qa/test_fun_death.py`'s storage seam
- **Done when:** all five scenarios pass.
- **Gate:** `uv run pytest qa/test_x_drawing_idea.py -q`
- **Commit:** folded into T1's commit
- **→ R1.3, R2.1**

### T-final — Close out

- **Skills:** none
- **What:** `docs/contracts/x_drawing_idea.md`, `scripts/spec.py` →
  `Status.DONE`, `cb.py docs-sync`, feature-page prose, `HANDOFF.md`.
- **Where:** `docs/contracts/x_drawing_idea.md`, `scripts/spec.py`,
  `docs/site/content/docs/features/x_drawing_idea.mdx`, `HANDOFF.md`, this file
- **Depends on:** T2
- **Done when:** `uv run python scripts/cb.py check` exits 0.
- **Gate:** `uv run python scripts/cb.py check`
- **Commit:** folded into T1's commit
- **→ R1, R2**
