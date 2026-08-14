# x_analytics_api — Specify

**Feature id:** `x_analytics_api` · **Milestone:** M4 · **Kind:** net-new
**v1 source:** none. v1's Java backend has no analytics endpoint and v1's bot
collects no analytics; `/implement-feature`, not `/migrate-feature`.

## Goal

A group's admins can read their own group's numbers over HTTP: what happened
per day, which commands get used, what the AI costs, and one summary of the
three.

## Why this row existed before the endpoints did

The data has been there since M0. `message_events` is the fact table,
`cb_rollup_day` (migration `0001`) and `cb_rollup_llm_day` (`0002`) fold it
nightly into `group_daily_stats`, `command_daily_stats` and `llm_daily_cost`,
and `cb-worker` has been running both on a cron since. Grafana reads them
directly. What was missing was an HTTP surface for the people whose groups
those are — which is the row, and why its note said "rollup tables exist; no
HTTP surface yet".

## Scope

| In | Out |
|---|---|
| Per-group reads of the three rollup tables | Anything computed from `message_events` live — the rollups exist so a dashboard never touches the fact table |
| A bounded date window, defaulting to 30 days | Hourly or real-time resolution; the rollups are daily |
| The token `/login` already mints, plus `group_admins` membership | A new auth mechanism |
| A summary object over an already-fetched window | Cross-group or fleet-wide totals — see below |

**There is deliberately no "all groups" endpoint.** It would be both a
cross-tenant leak and, on Citus, a fan-out to every shard: the rollup tables
are distributed on `group_id`, so a query without it is a repartition. Fleet
numbers are the operator's, and they already exist in Grafana against the same
tables.

## Behaviour contract

| Aspect | Behaviour |
|---|---|
| Endpoints | `GET /groups/{group_id}/analytics/{daily,commands,llm,summary}` |
| Auth | `Authorization: Bearer <token>` — RS256, verified against every key `/.well-known/jwks.json` publishes so a rotation does not invalidate live tokens |
| Authorisation | a row in `group_admins` for that group, or a tenant owner (`Tenant.owns`) |
| Denial | **404, not 403**, for a group the caller does not administer — whether a chat id is known here is not something an arbitrary logged-in user should be able to probe |
| Window | `start`/`end`, inclusive, UTC dates. Neither given: the last 30 days. One given: 30 days from it. Reversed, or wider than 366 days: 400 |
| `commands` limit | 1..100, default 20, busiest first |
| Missing days | absent, not zero-filled — the rollup writes only what it saw, and a fabricated zero row is indistinguishable from a real quiet day |
| `captcha_solve_rate` | `null` when nothing was issued: "nobody was asked" and "nobody solved it" are different facts |
| Percentiles | the window's **worst** day, never an average of percentiles |
| Active users | the **peak** day, never a sum — summing dailies counts the same person once per day |

## Defects this cannot repeat

The Java service's own list endpoints were unbounded `findAll()` (FEATURE-MAP
D11), which is why every endpoint here takes a window and a capped limit. Its
health/metrics were anonymous (D12) and its CORS was `*` with credentials
(D13); both are already fixed deployment-wide in `cb_api.main`, and this
feature inherits them rather than restating them.

## QA

No upstream scenario exists — QA describes v1, and v1 has no analytics.
Coverage is unit (window resolution, summary arithmetic), HTTP (auth,
authorisation, bodies) and integration (the three queries against real rollup
rows, including `Task Count: 1` for each).
