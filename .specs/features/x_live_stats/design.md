# x_live_stats — Design

## R1 — Worker (`packages/cb-worker/src/cb_worker/main.py`)

- **R1.1** New job `rollup_today`: calls `cb_rollup_day(today_utc)` and
  `cb_rollup_llm_day(today_utc)`. Registered as `cron(rollup_today, second=0)` (every
  minute) with `unique=True` / no overlap, using arq's idiom already used in the file. A
  run that is still going must not start a second one; if arq has no overlap guard, use
  the job id / `keep_result` idiom the file already uses for the other crons.
- **R1.2** `rollup_yesterday` / `rollup_llm_costs` compute days in UTC (`Instant.now()`
  as UTC date, not `to_system_tz()`). They keep running at 00:20/00:25 to close
  yesterday.
- **R1.3** Log duration at debug level; reuse the existing job metrics (no new labels
  with group/user ids).

## R2 — Live today for per-group analytics (`packages/cb-core/src/cb_core/analytics.py`)

- **R2.1** When the window includes today UTC, `daily()` replaces/adds today's row with
  one computed from `message_events WHERE group_id=$1 AND ts >= today_utc AND ts <
  tomorrow_utc`, using **the same aggregate expressions as `cb_rollup_day`** (copy them
  from `migrations/versions/0001_initial_schema.py:421-479` so the numbers match the
  rollup exactly: messages, commands, joins, leaves, captcha_issued, captcha_solved,
  active_users, errors, p95_latency_ms). LLM tokens/cost for today come from
  `llm_usage` with the same expressions as `cb_rollup_llm_day` (0002:166-198).
- **R2.2** `commands()` and `llm_costs()` merge today's live rows into the rollup rows
  for days < today (sum per command / per provider+model; p95 = max of the per-day p95s,
  which is what the existing multi-day aggregate already does — verify and keep it
  consistent).
- **R2.3** `summarise()` derives from the merged rows, so it needs no change once R2.1/2
  land.
- **R2.4** Every live query keeps `group_id = $1` first: single shard, `Task Count: 1`
  (integration test asserts it).
- **R2.5** The rollup queries exclude `day = today` when the live path is used, so there
  is no double counting.

## R3 — Fleet (`platform_analytics.py`)

No code change: the 1-minute rollup feeds it. Document freshness in the module
docstring and `miniapp-api.mdx`.

## Open decisions

| # | Question | Answer |
|---|---|---|
| O1 | Live raw reads for fleet too? | No: cross-shard scan of today's partition per request. Rollup each minute is enough. |
| O2 | Rollup interval | 1 min. Today's partition is row storage; rollup is shard-local GROUP BY. |
| O3 | Push vs poll | Poll from the WebHub (30 s while visible). |
