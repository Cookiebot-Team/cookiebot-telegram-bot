# x_live_stats — Spec

## Goal

Stats in the WebHub reflect bot activity within about a minute, not the next day.

Observed on UAT (2026-10-04): `message_events` had today's rows; `group_daily_stats` had
**zero** rows. Every analytics endpoint reads only the rollups, which the worker fills
once a day (`cb-worker/src/cb_worker/main.py:236-239`, `cron(rollup_yesterday, hour=0,
minute=20)`), so a fresh interaction never shows.

## Scope

In:
- Worker: roll up *today* (UTC) every minute for `group_daily_stats`,
  `command_daily_stats` and `llm_daily_cost`. The 00:20 / 00:25 jobs stay for closing
  yesterday.
- Worker: compute the rollup `day` in UTC (it currently uses `to_system_tz()`, while the
  API windows are UTC).
- Per-group analytics (`/groups/{id}/analytics/{daily,summary,commands,llm}`): today's
  numbers are computed live from the raw tables (`message_events`, `llm_usage`) for
  `day = today UTC`, so they are exact even between rollup ticks. Earlier days still come
  from the rollups.
- Fleet `/admin/*` analytics: rely on the 1-minute rollup (no cross-shard raw scans).

Out:
- Push transports (SSE/websocket). The WebHub polls instead (spec in
  `COOKIEBOT-WebHub/.specs/features/admin-insights/`, follow-up task).
- Live Valkey counters (a second source of truth; AGENTS.md §8).
- Private-chat activity (never recorded by design: `middlewares.py:68-78`).

## Behaviour contract

Net-new behaviour (v1 had no analytics), so there are no v1 `file:line` references.

| Aspect | Contract |
|---|---|
| Triggers | worker cron every minute; any per-group analytics GET whose window includes today (UTC) |
| Freshness | per-group: live for today; fleet: ≤ ~1 min + rollup duration |
| Output shape | unchanged on every endpoint |
| Persistence | rollup upserts (idempotent, existing `cb_rollup_day` / `cb_rollup_llm_day`) |
| Side effects | none new |
| Known defects | D1: rollups only nightly → **fix**. D2: worker uses the system TZ for `day` → **fix** (UTC). |
