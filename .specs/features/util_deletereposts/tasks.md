# util_deletereposts — Tasks

Depends on `util_postforwarder`'s T1 for the table and its repository.

## Status

| Task | Status | Notes |
|------|--------|-------|
| T1 — The `/deleteposts` handler | ⏳ not started | |
| T2 [P] — Unit + integration tests | ⏳ not started | |
| T3 — Acceptance | ⏳ not started | |
| T-final — Close out | ⏳ not started | |

### T1 — The `/deleteposts` handler

- **Skills:** /migrate-feature
- **What:** `handlers/deletereposts.py` per design R1–R4: `CommandName("deletereposts")`
  (all three spellings already alias to it), group-only, `ctx.is_admin` gate
  with no `owner_id` bypass (R1.2, R1.3), `not_group_admin` on refusal, then
  one `DELETE … WHERE requester_chat_id = $1` carrying R2.2's fan-out comment,
  the row count logged, 👍 then `deletereposts_done` in v1's order (R3.1), no
  chat action (R3.2). Telemetry per R4.1.
- **Where:** `packages/cb-gateway/src/cb_gateway/handlers/deletereposts.py` (new),
  `packages/cb-gateway/src/cb_gateway/handlers/__init__.py` (one router line),
  `packages/cb-core/src/cb_core/scheduled_posts.py` (`delete_by_requester`, if
  `util_postforwarder` T1 has not already added it)
- **Depends on:** `util_postforwarder` T1, T2
- **Reuses:** `cb_gateway.context.context_for`/`t`, `cb_gateway.filters.CommandName`
- **Done when:** an admin cancels every row this chat requested, across every
  target group; a non-admin gets `not_group_admin` and nothing is deleted.
- **Gate:** `uv run mypy packages/cb-gateway/src/cb_gateway/handlers/deletereposts.py`
- **Commit:** `feat(util_deletereposts): cancel the posts a group asked for`
- **→ R1, R2, R3, R4**

### T2 [P] — Unit + integration tests

- **Skills:** /migrate-feature
- **What:** Unit: all three trigger spellings resolve to the canonical name;
  the admin gate refuses a plain member and accepts an anonymous admin; the
  refusal string per language. Integration (real Citus): rows requested by
  chat A targeting groups B and C are both deleted, and a row requested by
  chat D is not.
- **Where:** `packages/cb-gateway/tests/test_deletereposts.py` (new),
  `qa/integration/test_scheduled_posts.py` (added cases)
- **Depends on:** T1
- **Gate:** `uv run pytest packages/cb-gateway/tests/test_deletereposts.py -q`
- **Commit:** folded into T1's commit
- **→ R1, R2**

### T3 — Acceptance

- **Skills:** /migrate-feature
- **What:** `qa/features/util_deletereposts.feature` — both QA scenarios,
  with scenario 2's `Then` corrected to v1's actual string and scenario 1's
  wording tightened from "all posts … are deleted" to "all *scheduled* posts"
  (spec.md conflicts 2 and 3). Real database, since what is deleted *is* the
  rows.
- **Where:** `qa/features/util_deletereposts.feature` (new),
  `qa/test_util_deletereposts.py` (new)
- **Depends on:** T1, T2
- **Gate:** `uv run pytest qa/test_util_deletereposts.py -q`
- **Commit:** `test(util_deletereposts): the cancel, and the message QA got wrong`
- **→ R1, R2, R3**

### T-final — Close out

- **Skills:** /migrate-feature, /review-changes, /lint-code
- **What:** `docs/contracts/util_deletereposts.md` with the Phase-2 and
  Phase-6 tables and D-DR-1/D-DR-2's verdicts; `scripts/spec.py` → `done`;
  `cb.py docs-sync`; the `.mdx` prose; both QA conflicts in `feature-map.mdx`.
- **Where:** `docs/contracts/util_deletereposts.md`, `scripts/spec.py`,
  `docs/site/content/docs/feature-map.mdx`
- **Depends on:** T1–T3
- **Gate:** `uv run python scripts/cb.py check`
- **Commit:** `docs(util_deletereposts): close out`
