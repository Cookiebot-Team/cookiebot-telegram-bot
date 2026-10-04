# x_admin_audit — Design

## R1 — Query layer (`packages/cb-core/src/cb_core/audit.py`)

- **R1.1** `page()` gains keyword-only `surface: Surface | None`, `since: datetime | None`,
  `until: datetime | None`. Every predicate is parameterised; `group_id` stays the
  first predicate so the read is still router-pruned to one shard (`Task Count: 1`).
- **R1.2** Over-fetch `limit + 1` rows; return the first `limit` and set the next
  cursor only when the extra row existed. Callers stop deriving `next_before`
  from `len == limit`; the function returns it (e.g. a small `AuditSlice(events,
  next_before)` struct, or a tuple — match the module's existing idiom).
- **R1.3** New `fleet_page(conn, *, tenant_id, limit, before, group_id, action,
  actor_user_id, surface, since, until)` — the one cross-shard audit read. Joins
  `groups` (colocated on `group_id`, so the join is pushed down per shard) for
  `title`, and scopes to the tenant exactly the way `cb_core/platform_analytics.py`
  scopes its reads (same column/predicate; reuse its helper if one exists). With
  `group_id` given it must be single-shard. Ordering `id DESC` (UUIDv7 ⇒ time
  order), `before` exclusive, same `limit + 1` rule. Docstring argues why a
  multi-shard read is acceptable here (owner-only, off the reply path, keyset +
  LIMIT so each shard returns ≤ limit+1 rows) — mirror the platform_analytics
  docstring.
- **R1.4** `since`/`until`: filter on `ts` (`ts >= since AND ts < until`). Naive
  datetimes are treated as UTC.

## R2 — Migration `0011_audit_fleet_index`

- **R2.1** `CREATE INDEX IF NOT EXISTS group_audit_events_id_desc ON
  group_audit_events (id DESC)` — lets each shard serve the fleet keyset read
  (`ORDER BY id DESC LIMIT n`) without a sort. Citus propagates the index to
  shards. Downgrade drops it. Raw SQL in `op.execute`, as in `0010`.
- **R2.2** `CREATE INDEX IF NOT EXISTS group_audit_events_surface ON
  group_audit_events (group_id, surface, id DESC)` — `group_id` first (AGENTS.md §4).

## R3 — Group endpoint (`packages/cb-api/src/cb_api/routers/groups.py`, ~line 485)

- **R3.1** New query params `surface`, `since`, `until`, typed with the existing
  enums / `datetime`; descriptions api-lint clean.
- **R3.2** `since >= until` → `400 {"detail": "invalid_window"}` (match the
  analytics router's error idiom in `resolve_window`).
- **R3.3** `next_before` comes from R1.2. Response model unchanged.

## R4 — Admin endpoint (`packages/cb-api/src/cb_api/routers/admin.py`)

- **R4.1** `GET /admin/audit`, dependency `bot_admin_caller("admin:read", "audit:read")`.
  Query: `limit` (1-100, default 50), `before`, `group_id`, `action`,
  `actor_user_id`, `surface`, `since`, `until`.
- **R4.2** Response `AdminAuditPage {events: list[AdminAuditEvent], next_before:
  UUID | None}`; `AdminAuditEvent` = all `AuditEvent` fields + `group_id: int`
  + `group_title: str | None`. Pydantic models next to the existing admin models.
- **R4.3** Same 400 `invalid_window` rule as R3.2.
- **R4.4** Registration: `MINIAPP_PATHS` row in
  `packages/cb-api/tests/test_openapi.py`, a contract `Case` in
  `qa/api/test_contract.py`, regenerate `docs/site/public/openapi.json` via
  `uv run python scripts/cb.py api-docs`, and a section in
  `docs/site/content/docs/miniapp-api.mdx`.

## Telemetry

No new metrics. Existing request metrics cover both routes; never label with
`group_id`/`user_id`.

## Open decisions

| # | Question | Answer |
|---|---|---|
| O1 | Offset vs keyset paging? | Keyset (D11 rule). The UI keeps a cursor stack for "newer". |
| O2 | Should `/admin/audit` need `audit:read` too? | Yes — both are in an owner's default scopes; it keeps the audit permission explicit. |
| O3 | Filter on `id` time-prefix vs `ts`? | `ts` — explicit, and `id`/`ts` are written together. |
| O4 | Return total counts? | No — counting is a full scan across shards. UI shows page number, not "of N". |
