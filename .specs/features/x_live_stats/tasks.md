# x_live_stats — Tasks

Grammar: `tlc-spec-driven` §5. Spec: `spec.md`, design: `design.md` (same dir).
Branch: `feat/live-stats`. Stage only the paths in **Where** (the tree has unrelated untracked files).

## Status

| Task | Status | Notes |
|------|--------|-------|
| T1 [P] — Minute rollup for today, UTC days | ✅ done | R1; fixed job_id + 55s timeout for no-overlap |
| T2 [P] — Live today in per-group analytics | ✅ done | R2; daily LLM tokens/cost from message_events (matches cb_rollup_day), not llm_usage |
| T-final — Close out | ✅ done | spec row, docs-sync, contract, freshness notes |

## Tasks

### T1 [P] — Minute rollup for today, UTC days

- **Skills:** /implement-feature
- **What:** design R1.1–R1.3. Unit test: the cron table has the new job with the
  every-minute schedule; the day passed to the SQL is the UTC date even when the
  system TZ is not UTC (monkeypatch TZ).
- **Where:** `packages/cb-worker/src/cb_worker/main.py`, `packages/cb-worker/tests/`
  (existing worker test module for crons, or a new `test_rollup_schedule.py`)
- **Depends on:** none
- **Reuses:** `rollup_yesterday`, `rollup_llm_costs`
- **Done when:** gate green.
- **Gate:** `uv run pytest -q packages/cb-worker/tests`
- **Commit:** `feat(x_live_stats): roll up today every minute, in UTC`
- **→ R1.1–R1.3**

### T2 [P] — Live today in per-group analytics

- **Skills:** /implement-feature
- **What:** design R2.1–R2.5. Integration tests (Citus DB at localhost:5432):
  seed raw `message_events`/`llm_usage` for today with **no** rollup → endpoints'
  query functions return today's numbers; seed yesterday via `cb_rollup_day` + today raw
  → no double count; after running `cb_rollup_day(today)` the live and rolled-up numbers
  are identical (parity); EXPLAIN on the live queries shows `Task Count: 1`.
- **Where:** `packages/cb-core/src/cb_core/analytics.py`,
  `qa/integration/test_analytics.py`
- **Depends on:** none
- **Reuses:** aggregate expressions in `0001_initial_schema.py:421-479` and
  `0002_media_and_llm_usage.py:166-198`; `cb_core/llm/budget.py:140-195` (rollup + live
  today precedent); `qa/integration/factories.py`
- **Done when:** integration + analytics unit tests green.
- **Gate:** `uv run pytest -q -m integration qa/integration/test_analytics.py && uv run pytest -q packages/cb-api/tests/test_analytics_endpoints.py packages/cb-api/tests/test_analytics_window.py`
- **Commit:** `feat(x_live_stats): today's group stats computed live from raw events`
- **→ R2.1–R2.5**

### T-final — Close out

- **Skills:** none
- **What:** `spec.py` row `x_live_stats` (platform, M4, DONE, Layer.API); `cb.py
  docs-sync`; `docs/contracts/x_live_stats.md`; freshness note in
  `docs/site/content/docs/miniapp-api.mdx` and the `platform_analytics.py` docstring.
- **Where:** `scripts/spec.py`, `docs/contracts/x_live_stats.md`, `docs/site/content/**`,
  `packages/cb-core/src/cb_core/platform_analytics.py`, `.specs/features/x_live_stats/tasks.md`
- **Depends on:** T1, T2
- **Gate:** `uv run python scripts/cb.py check`. The known local-only failures (an untracked doc, the bench baseline) are accepted.
- **Commit:** `docs(x_live_stats): close out`
