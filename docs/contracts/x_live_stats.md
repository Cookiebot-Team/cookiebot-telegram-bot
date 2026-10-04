# Contract: x_live_stats (net-new)

**No v1 equivalent.** v1 had no analytics, so there is no v1 `file:line` for any
row; this file records the contract v2 is now committed to.
FEATURE-MAP row: `x_live_stats`. Spec: `.specs/features/x_live_stats/`.
Files owned by this feature:
`packages/cb-worker/src/cb_worker/main.py` (`rollup_today`, UTC days),
`packages/cb-core/src/cb_core/analytics.py` (live today),
`packages/cb-core/src/cb_core/platform_analytics.py` (docstring only),
`docs/site/content/docs/miniapp-api.mdx` (freshness note), the tests, this file.

## Behaviour contract

| Aspect | Contract |
|---|---|
| Triggers | Worker cron every minute (`rollup_today`); any `GET /groups/{group_id}/analytics/{daily,summary,commands,llm}` whose window includes today (UTC) |
| Preconditions | Unchanged: caller administers the group and token has `groups:read` |
| Cooldowns | `rollup_today` never overlaps itself (fixed job id, 55 s timeout) |
| Success output | Shapes unchanged on every endpoint |
| Freshness | Per-group: live for today (exact). `/admin/*`: at most about 1 min + rollup duration |
| Failure output | Unchanged |
| Persistence | Rollup upserts, idempotent (`cb_rollup_day`, `cb_rollup_llm_day`); no schema change |
| Side effects | none new |
| External calls | none |
| Known defects | D1: rollups only nightly - **fixed**. D2: worker used the system TZ for `day` while API windows are UTC - **fixed** (UTC). |

## Parity with v1

| v1 behaviour | v2 | Verdict |
|---|---|---|
| (none - v1 had no analytics) | net-new, no v1 | n/a |

## Rules this feature is bound by

* **AGENTS.md §4.1 - every query filters on the distribution column.** Every
  live query keeps `group_id = $1` first: single shard, `Task Count: 1`
  (asserted in the integration test). Fleet analytics deliberately get no live
  raw scan (a cross-shard read per request); the minute rollup serves them.
* **No double counting.** Rollup reads exclude `day = today` when the live path
  is used, so a row the minute job already wrote for today is ignored.
* **Parity.** Live aggregates use the same expressions as `cb_rollup_day` /
  `cb_rollup_llm_day`; daily LLM tokens/cost come from `message_events`
  (as the rollup does), per-model rows from `llm_usage`.

## Tests

| Layer | File |
|---|---|
| Unit - cron table (every minute, no overlap), UTC day, closing crons kept | `packages/cb-worker/tests/test_rollup_schedule.py` |
| Integration - live with no rollup, no double count, live/rollup parity, `Task Count: 1` | `qa/integration/test_analytics.py` |
| HTTP - analytics endpoints and window | `packages/cb-api/tests/test_analytics_endpoints.py`, `test_analytics_window.py` |

No acceptance scenario: no Telegram surface.
