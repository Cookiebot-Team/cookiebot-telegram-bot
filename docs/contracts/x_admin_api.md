# Contract: x_admin_api (net-new)

**No v1 equivalent.** The Java backend had no analytics endpoint of any kind
and v1's bot collected no analytics, so there is nothing to be backwards
compatible with — this file records the contract v2 is now committed to.
FEATURE-MAP row: `x_admin_api`. Files owned by this feature:
`packages/cb-core/src/cb_core/platform_analytics.py` (new),
`packages/cb-api/src/cb_api/routers/admin.py` (new),
`packages/cb-api/src/cb_api/security.py` (`is_bot_admin`, `bot_admin_caller`),
`packages/cb-api/src/cb_api/routers/oauth.py` (`_scopes_for`),
`packages/cb-api/src/cb_api/routers/groups.py` (`/me`'s `is_bot_admin`),
`packages/cb-core/src/cb_core/settings.py` (`miniapp_admin_scopes`),
`packages/cb-api/src/cb_api/main.py` (one router line), the tests, this file.

## What existed before it

`x_analytics_api` answers "how is **my group** doing" and refuses, by
construction, to answer anything wider: every query it makes carries a
`group_id` and the caller must administer that group. That is right for a group
admin and useless to the person who runs the deployment, who could not see how
many groups the bot is in, which of them are alive, what the fleet costs in LLM
tokens, or which commands anyone actually uses. Those numbers were in Grafana,
which is not something a Mini App can open.

## The contract

| Aspect | Behaviour |
|---|---|
| Endpoints | `GET /admin/overview`, `/admin/analytics/daily`, `/admin/analytics/groups`, `/admin/analytics/commands`, `/admin/analytics/llm`, `/admin/groups`, `/admin/tenant` |
| Authentication | `Authorization: Bearer <token>` — the same RS256 token every other endpoint takes |
| Authorisation | a tenant owner (`Tenant.owns`) **or** `CB_OWNER_ID`, which is what the owner-only Telegram commands answer to, **and** the `admin:read` scope |
| Denial | **403, not 404** — the opposite of the group endpoints, and deliberate: `/admin/…` is a fixed path in `/openapi.json`, so there is no chat id to hide, and a 404 would only mislead an owner holding a stale token |
| Scope grant | `admin:read` is added to a session **at token issue time**, only when the subject is an owner (`CB_MINIAPP_ADMIN_SCOPES`, default `["admin:read"]`) |
| Scope on refresh | reissued as stored, never re-evaluated — an owner removed today keeps it until the refresh token expires or the session is revoked |
| `/me` | carries `is_bot_admin`, read from the tenant rather than inferred from the token's scopes |
| Window | identical to `x_analytics_api`: inclusive UTC dates, 30-day default, **400** past 366 days or reversed |
| Directory pagination | keyset on `group_id` (`after`), `limit` 1..200, `next_after` null on the last page |
| Writes | none. Every endpoint is a read |
| Member data | none. No message, no member name, no group's rules — an owner who wants a group's settings calls `/groups/{id}/config` like anybody else |
| Bot tokens | never returned by `/admin/tenant` |

## Rules this feature is bound by, and the one it departs from

* **AGENTS.md §4.1 — every query filters on the distribution column.** These
  queries **do not**, and that is the feature's one deliberate departure. It is
  bounded by three facts, written out in `cb_core/platform_analytics.py`'s
  docstring: they read the daily *rollups* (one row per group per day, not per
  message), every one of them aggregates before returning so only the grouped
  result crosses the network, and nothing on the reply path calls them. The
  fan-out is confined to that module so it can be found again; if a deployment
  ever outgrows it, the fix is a `platform_daily_stats` rollup in `cb-worker`,
  and the shapes here are already what it would return.
* **No unbounded list** (D11): every endpoint is bounded by a window, and the
  two list endpoints by a capped `limit` and a keyset cursor.
* **cb-api never calls Telegram.** Ownership is read from `tenants` and
  settings, never refreshed over the Bot API.
* **Granted, not assumed.** The scope is decided once, where the deployment
  decides what a session may do, so a non-owner's token cannot reach these
  endpoints however the client edits its own request.

## Tests

| Layer | File |
|---|---|
| HTTP — the boundary, the shapes, the windows, and that every route is behind the dependency | `packages/cb-api/tests/test_admin_endpoints.py` |
| HTTP — the scope grant, and that a refresh reissues it unchanged | `packages/cb-api/tests/test_oauth_endpoints.py` |
| HTTP — `/me`'s `is_bot_admin` | `packages/cb-api/tests/test_group_endpoints.py` |
| Schema — every path described, and 403-not-404 asserted from `/openapi.json` | `packages/cb-api/tests/test_openapi.py` |
| Integration — the six fleet queries against real rollup rows in real Citus, and that the plan aggregates rather than shipping rows | `qa/integration/test_platform_analytics.py` |
| Contract — every response validated against the published `openapi.json`, refusals included | `qa/api/test_contract.py` |
| API integration — the real app over ASGI: the boundary, the directory's keyset cursor, the budget | `qa/api/test_integration.py` |
| Smoke — a running deployment answers, and still refuses a group admin | `qa/api/test_smoke.py` |

No acceptance scenario: this feature has no Telegram surface. The QA suite
drives handlers through a mock Telegram, and there is no command to send.
