# Contract: x_admin_audit (net-new)

**No v1 equivalent.** v1 kept no audit trail, so there is no v1 `file:line` for
any row; this file records the contract v2 is now committed to.
FEATURE-MAP row: `x_admin_audit`. Spec: `.specs/features/x_admin_audit/`.
Files owned by this feature:
`packages/cb-api/migrations/versions/0011_audit_fleet_index.py` (new),
`packages/cb-core/src/cb_core/audit.py` (filters, exact cursor, `fleet_page`),
`packages/cb-api/src/cb_api/routers/groups.py` (group endpoint filters),
`packages/cb-api/src/cb_api/routers/admin.py` (`GET /admin/audit`), the tests,
`docs/site/public/openapi.json`, this file.

## Behaviour contract

| Aspect | Contract |
|---|---|
| Triggers | `GET /groups/{group_id}/audit`, `GET /admin/audit` |
| Preconditions | Group endpoint: caller administers the group (else **404**) and token has `audit:read` (else **403** `insufficient_scope`). Admin endpoint: caller is a bot admin (else **403**) and token has `admin:read` **and** `audit:read`. |
| Cooldowns | none |
| Success output | Group: `AuditPage {group_id, events[], next_before}` (unchanged shape). Admin: `AdminAuditPage {events[], next_before}` where each event is the group event plus `group_id` and `group_title` (nullable). Newest first, ordered by `id DESC`. |
| Filters | Group: `action`, `actor_user_id`, `surface`, `since`, `until`. Admin: the same plus `group_id`. `since` inclusive, `until` exclusive, both ISO-8601 datetimes (naive = UTC). All optional; combinable (AND). |
| Paging | `limit` 1-100, default 50; `before` = UUIDv7 cursor (exclusive). `next_before` = id of the last returned event **iff** at least one older matching row exists, else `null`. |
| Failure output | `since >= until` -> **400** `invalid_window`. Unknown `action`/`surface` -> **422** (enum-validated). Bad UUID cursor -> **422**. |
| Persistence | Read-only. Migration `0011` adds indexes only (`group_audit_events_id_desc` on `id DESC`, `group_audit_events_surface` on `group_id, surface, id DESC`). |
| Side effects | none |
| External calls | none |
| Known defects | D1: trailing empty page (`next_before` with no older rows) - **fixed**. D2: `administers()` ignores `CB_OWNER_ID` - **preserved** (owners use `/admin/audit?group_id=`). |

## Parity with v1

| v1 behaviour | v2 | Verdict |
|---|---|---|
| (none - v1 kept no audit trail) | net-new, no v1 | n/a |

## Rules this feature is bound by, and the one it departs from

* **AGENTS.md §4.1 - every query filters on the distribution column.**
  `GET /admin/audit` without `group_id` does not: it is the second deliberate
  cross-shard read after `x_admin_api`. Bounded by keyset + `LIMIT` (each shard
  returns at most `limit + 1` rows, served by the `id DESC` index), owner-only,
  tenant-scoped through `groups.tenant_id` (the join to `groups` is colocated),
  and off the reply path. With `group_id` it is single-shard (`Task Count: 1`).
  The argument lives in `cb_core/audit.py`'s `fleet_page` docstring.
* **403, not 404, on `/admin/audit`**, as for the rest of `/admin`.

## Tests

| Layer | File |
|---|---|
| Integration - filters, exact cursor (D1), fleet paging, tenant isolation, EXPLAIN `Task Count: 1` | `qa/integration/test_audit_log.py` |
| HTTP - group endpoint filters, 400 window, null cursor | `packages/cb-api/tests/test_group_endpoints.py` |
| HTTP - owner boundary, scopes, shape incl. `group_title` | `packages/cb-api/tests/test_admin_endpoints.py` |
| Schema - path registered and described | `packages/cb-api/tests/test_openapi.py` |
| Contract - responses validated against `openapi.json` | `qa/api/test_contract.py` |

No acceptance scenario: no Telegram surface.
