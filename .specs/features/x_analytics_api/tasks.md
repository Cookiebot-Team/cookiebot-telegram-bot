# x_analytics_api — Tasks

Grammar: `tlc-spec-driven` §5.

## Status

| Task | Status | Notes |
|------|--------|-------|
| T1 — Query layer in cb-core | ✅ done | R1 |
| T2 — Token verification and the group-admin gate | ✅ done | R2, R3 |
| T3 — The four endpoints | ✅ done | R4 |
| T4 — Integration coverage | ✅ done | real rollup rows + Task Count: 1 |
| T-final — Close out | ✅ done | spec row PLANNED → DONE |

## Tasks

### T1 — Query layer in cb-core

- **Skills:** /implement-feature
- **What:** `daily`, `commands`, `llm_costs`, `summarise` and their structs
  (R1), every query carrying `group_id`.
- **Where:** `packages/cb-core/src/cb_core/analytics.py`
- **Depends on:** none
- **Reuses:** `cb_core.db`, the rollup tables from migrations `0001`/`0002`
- **Done when:** the three queries return typed rows and `summarise` folds them.
- **Gate:** `uv run pytest packages/cb-api/tests/test_analytics_window.py -q`
- **Commit:** folded into T3's commit
- **→ R1.1-R1.5**

### T2 — Token verification and the group-admin gate

- **Skills:** /implement-feature
- **What:** `current_user` and `group_admin` (R2, R3), plus `keys.public_pem`.
- **Where:** `packages/cb-api/src/cb_api/security.py`,
  `packages/cb-api/src/cb_api/keys.py`
- **Depends on:** none
- **Reuses:** `cb_api.keys.published_keys`, `cb_core.tenancy`
- **Done when:** a stranger's token gets 404 and an admin's gets 200.
- **Gate:** `uv run pytest packages/cb-api/tests/test_analytics_endpoints.py -q`
- **Commit:** folded into T3's commit
- **→ R2.1-R3.3**

### T3 — The four endpoints

- **Skills:** /implement-feature
- **What:** `daily`, `commands`, `llm`, `summary`, the window rules (R4), and
  registration in `cb_api.main`.
- **Where:** `packages/cb-api/src/cb_api/routers/analytics.py`,
  `packages/cb-api/src/cb_api/main.py`,
  `packages/cb-api/tests/test_analytics_{window,endpoints}.py`
- **Depends on:** T1, T2
- **Done when:** all four answer for an admin and 404 for anyone else.
- **Gate:** `uv run pytest packages/cb-api -q`
- **Commit:** `feat(x_analytics_api): four endpoints over the rollups nobody could read`
- **→ R4.1-R4.3**

### T4 — Integration coverage

- **Skills:** /implement-feature (Phase 5)
- **What:** the three queries against real rollup rows in Citus, cross-group
  isolation, and `Task Count: 1` for each.
- **Where:** `qa/integration/test_analytics.py`
- **Depends on:** T3
- **Gate:** `uv run pytest qa/integration/test_analytics.py -q`
- **Commit:** folded into T3's commit
- **→ R1.1**

### T-final — Close out

- **Skills:** none
- **What:** `docs/contracts/x_analytics_api.md`, `scripts/spec.py` →
  `Status.DONE`, `cb.py docs-sync`, feature-page prose.
- **Where:** `docs/contracts/x_analytics_api.md`, `scripts/spec.py`,
  `docs/site/content/docs/features/x_analytics_api.mdx`, this file
- **Depends on:** T4
- **Gate:** `uv run python scripts/cb.py check`
- **Commit:** folded into T3's commit
