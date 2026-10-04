# x_admin_audit — Spec

## Goal

Make the audit trail browsable from the WebHub for both audiences, with filters
and keyset pages by default:

- **Group admins** — their own group's trail via `GET /groups/{group_id}/audit`
  (exists since `x_audit_log`), extended with surface and time-window filters.
- **Bot admins (owners)** — a fleet-wide trail via a new `GET /admin/audit`,
  filterable by group, action, actor, surface and time window.

The WebHub side (screens, ECharts stats, events fix) is specified in
`COOKIEBOT-WebHub/.specs/features/admin-insights/`. This spec owns only the API.

## Scope

In:
- New filters on the per-group endpoint: `surface`, `since`, `until`.
- Fix: `next_before` is only returned when an older row exists (today it is set
  whenever `len(events) == limit`, so the client gets a trailing empty page).
- New owner endpoint `GET /admin/audit`, tenant-scoped like the rest of `/admin`.
- Migration `0011` adding the index the fleet keyset read needs.

Out:
- Auditing moderation actions (captcha kicks, doomlist bans) — separate feature.
- Making `administers()` honour `CB_OWNER_ID` (known gap, recorded below; owners
  use `/admin/audit?group_id=` instead).
- Writing `session.started` (defined, no caller — unchanged).

## Behaviour contract

Net-new feature: v1 kept no audit trail, so there is no v1 `file:line` for any row.

| Aspect | Contract |
|---|---|
| Triggers | `GET /groups/{group_id}/audit`, `GET /admin/audit` |
| Preconditions | Group endpoint: caller administers the group (else **404**) and token has `audit:read` (else **403** `insufficient_scope`). Admin endpoint: caller is a bot admin (else **403**) and token has `admin:read` **and** `audit:read`. |
| Cooldowns | none |
| Success output | Group: `AuditPage {group_id, events[], next_before}` (unchanged shape). Admin: `AdminAuditPage {events[], next_before}` where each event is the group event plus `group_id` and `group_title` (nullable). Newest first, ordered by `id DESC`. |
| Filters | Group: `action`, `actor_user_id`, `surface`, `since`, `until`. Admin: the same plus `group_id`. `since` inclusive, `until` exclusive, both ISO-8601 datetimes (naive = UTC). All optional; combinable (AND). |
| Paging | `limit` 1-100, default 50; `before` = UUIDv7 cursor (exclusive). `next_before` = id of the last returned event **iff** at least one older matching row exists, else `null`. |
| Failure output | `since >= until` → **400** `invalid_window`. Unknown `action`/`surface` → **422** (enum-validated). Bad UUID cursor → **422**. |
| Persistence | Read-only. Migration `0011` adds an index only. |
| Side effects | none |
| External calls | none |
| Known defects | D1: trailing empty page (`next_before` with no older rows) — **fix**. D2: `administers()` ignores `CB_OWNER_ID` — **preserve** (out of scope, owners use `/admin/audit`). |

## QA vs v1 conflicts

None — no v1 behaviour and no QA scenario covers the audit trail yet.
