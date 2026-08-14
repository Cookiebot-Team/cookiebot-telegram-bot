# Contract: x_analytics_api (net-new)

Phase 2/6 of `/implement-feature`. **No v1 equivalent**: the Java backend has
no analytics endpoint and v1's bot collects no analytics, so there is nothing
to be backwards compatible with — this file records the contract v2 is now
committed to instead. FEATURE-MAP row: `x_analytics_api`. Spec/design:
`.specs/features/x_analytics_api/`. Files owned by this feature:
`packages/cb-core/src/cb_core/analytics.py` (new),
`packages/cb-api/src/cb_api/security.py` (new),
`packages/cb-api/src/cb_api/routers/analytics.py` (new),
`packages/cb-api/src/cb_api/keys.py` (`public_pem`),
`packages/cb-api/src/cb_api/main.py` (one router line), the tests, this file.

## What existed before it

Everything except the endpoint. `message_events` has been the fact table since
M0; `cb_rollup_day` (migration `0001`) and `cb_rollup_llm_day` (`0002`) fold it
nightly into `group_daily_stats`, `command_daily_stats` and `llm_daily_cost`;
`cb-worker` runs both on a cron. Grafana reads those tables directly. The
people whose groups they describe had no way to.

## The contract

| Aspect | Behaviour |
|---|---|
| Endpoints | `GET /groups/{group_id}/analytics/daily`, `/commands`, `/llm`, `/summary` |
| Authentication | `Authorization: Bearer <token>` — the token `/login` mints, RS256, verified against **every** key `/.well-known/jwks.json` publishes so a key rotation does not invalidate live tokens |
| Authorisation | a `group_admins` row for that group, or a tenant owner (`Tenant.owns`) |
| Denial | **404, not 403** — whether a chat id is known to this deployment is not something an arbitrary logged-in user should be able to probe |
| Window | `start`/`end`, inclusive UTC dates. Neither: last 30 days. One: 30 days from it. Reversed or wider than 366 days: **400**, never a silent clamp |
| `commands` limit | 1..100, default 20, busiest first, totalled across the window |
| Missing days | absent, never zero-filled — the rollup writes only what it saw |
| `captcha_solve_rate` | `null` when nothing was issued |
| `worst_p95_latency_ms` / command `p95_latency_ms` | the window's **worst** day, never an average of percentiles |
| `peak_active_users` | the **peak** day, never a sum |
| Costs | `numeric` in the database, `float` on the wire — converted once at the repository boundary |

## Rules this feature is bound by

* **Every query filters on `group_id`** (AGENTS.md §4), which here is also the
  authorisation boundary. There is deliberately **no fleet-wide endpoint**: it
  would be a cross-tenant leak and, on Citus, a repartition across every shard.
  `qa/integration/test_analytics.py` asserts `Task Count: 1` for all three
  queries.
* **No unbounded list** (FEATURE-MAP D11, the Java service's `findAll()`):
  every endpoint is bounded by a window, and the one list endpoint by a capped
  limit.
* **cb-api never calls Telegram.** Authorisation reads `group_admins` as the
  gateway maintains it; refreshing the admin list needs a `Bot`, and an HTTP
  read is not where "someone was promoted an hour ago" should be discovered.

## Tests

| Layer | File |
|---|---|
| Unit — window resolution, summary arithmetic | `packages/cb-api/tests/test_analytics_window.py` |
| HTTP — authentication, authorisation, the four bodies | `packages/cb-api/tests/test_analytics_endpoints.py` |
| Integration — the three queries against real rollup rows, cross-group isolation, `Task Count: 1` | `qa/integration/test_analytics.py` |

No acceptance scenario: this feature has no Telegram surface, and the QA suite
drives the bot. That is also why `scripts/spec.py` files it under `api`.
