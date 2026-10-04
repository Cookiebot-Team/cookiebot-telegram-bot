# x_admin_audit — Tasks

Grammar: `tlc-spec-driven` §5. Spec: `spec.md`, design: `design.md` (same dir).
Branch: `feat/admin-audit-viewer`. The working tree has unrelated untracked files —
stage only the paths in **Where**.

## Status

| Task | Status | Notes |
|------|--------|-------|
| T1 — Migration 0011 audit indexes | ✅ done | R2; up→down→up verified on Citus 13 |
| T2 — Query layer: filters, over-fetch, fleet_page | ✅ done | R1; fleet_page has no conn arg (db.fetch idiom) |
| T3 [P] — Group endpoint filters + cursor fix | ✅ done | R3; naive/aware window normalised, BAD_AUDIT_WINDOW |
| T4 [P] — GET /admin/audit | ✅ done | R4; tenant = DEFAULT_TENANT like other /admin routes |
| T-final — Close out | ✅ done | spec row, contract doc, docs-sync, cb.py check |

## Tasks

### T1 — Migration 0011 audit indexes

- **Skills:** /implement-feature
- **What:** New Alembic revision `0011` (down_revision = the `0010` revision id)
  creating the two indexes in design R2.1/R2.2 with raw SQL `op.execute`, and a
  downgrade dropping them. Follow `0010_miniapp_sessions_and_audit.py` style.
- **Where:** `packages/cb-api/migrations/versions/0011_audit_fleet_index.py`
- **Depends on:** none
- **Reuses:** migration `0010`
- **Done when:** upgrade → downgrade → upgrade succeeds against the test DB.
- **Gate:** `uv run pytest -q -m integration qa/integration/test_audit_log.py`
- **Commit:** `feat(x_admin_audit): index group_audit_events for the fleet keyset read`
- **→ R2.1, R2.2**

### T2 — Query layer: filters, over-fetch, fleet_page

- **Skills:** /implement-feature
- **What:** Extend `cb_core.audit.page()` with `surface`/`since`/`until` and the
  `limit + 1` over-fetch returning the next cursor (R1.1, R1.2, R1.4); add
  `fleet_page()` (R1.3) tenant-scoped like `cb_core/platform_analytics.py`.
  Update the existing caller in `routers/groups.py` only as much as needed to keep
  it compiling and behaving (the new params land in T3). Integration tests: each
  filter alone and combined; `next_before` is `null` on the exact-limit last page
  (D1); fleet read across ≥2 groups orders by `id DESC` and pages without
  duplicates/gaps; another tenant's rows never appear; `fleet_page(group_id=…)`
  EXPLAIN shows `Task Count: 1`; group `page()` still `Task Count: 1`.
- **Where:** `packages/cb-core/src/cb_core/audit.py`,
  `packages/cb-api/src/cb_api/routers/groups.py`,
  `qa/integration/test_audit_log.py`
- **Depends on:** T1
- **Reuses:** `qa/integration/factories.py`, `cb_core.ids.uuid7`,
  tenant predicate from `cb_core/platform_analytics.py`
- **Done when:** integration suite above green; unit tests still green.
- **Gate:** `uv run pytest -q -m integration qa/integration/test_audit_log.py && uv run pytest -q packages/cb-api/tests/test_group_endpoints.py`
- **Commit:** `feat(x_admin_audit): audit filters, exact next cursor and the fleet page`
- **→ R1.1-R1.4**

### T3 [P] — Group endpoint filters + cursor fix

- **Skills:** /implement-feature
- **What:** Add `surface`, `since`, `until` query params to
  `GET /groups/{group_id}/audit` (R3.1), `400 invalid_window` when
  `since >= until` (R3.2), take `next_before` from the query layer (R3.3).
  Unit tests for each new param, the 400, and the null cursor on the last page.
  Add/extend the contract `Case` for the new params. Run
  `uv run python scripts/cb.py api-docs` and commit the regenerated
  `docs/site/public/openapi.json`.
- **Where:** `packages/cb-api/src/cb_api/routers/groups.py`,
  `packages/cb-api/tests/test_group_endpoints.py`, `qa/api/test_contract.py`,
  `docs/site/public/openapi.json`
- **Depends on:** T2
- **Reuses:** `resolve_window` error idiom in `routers/analytics.py`
- **Done when:** the endpoint accepts the filters; OpenAPI regenerated; api-lint clean.
- **Gate:** `uv run pytest -q packages/cb-api/tests/test_group_endpoints.py packages/cb-api/tests/test_openapi.py && uv run python scripts/cb.py api-lint`
- **Commit:** `feat(x_admin_audit): surface and time-window filters on the group audit`
- **→ R3.1-R3.3**

### T4 [P] — GET /admin/audit

- **Skills:** /implement-feature
- **What:** New owner endpoint per R4.1-R4.3 calling `fleet_page()`. Unit tests:
  non-owner 403, missing `audit:read` 403 `insufficient_scope`, filters passed
  through, 400 window, response shape incl. `group_title`. Register per R4.4
  (`MINIAPP_PATHS`, contract `Case`, `cb.py api-docs`, `miniapp-api.mdx`
  section). If T3 already regenerated `openapi.json`, regenerate again on top.
- **Where:** `packages/cb-api/src/cb_api/routers/admin.py`,
  `packages/cb-api/tests/test_admin_endpoints.py`,
  `packages/cb-api/tests/test_openapi.py`, `qa/api/test_contract.py`,
  `docs/site/public/openapi.json`, `docs/site/content/docs/miniapp-api.mdx`
- **Depends on:** T2
- **Reuses:** `bot_admin_caller`, existing admin Pydantic models, `AuditEvent`
- **Done when:** endpoint live in OpenAPI; api-lint clean; tests green.
- **Gate:** `uv run pytest -q packages/cb-api/tests/test_admin_endpoints.py packages/cb-api/tests/test_openapi.py && uv run python scripts/cb.py api-lint`
- **Commit:** `feat(x_admin_audit): fleet-wide audit trail for bot owners`
- **→ R4.1-R4.4**

### T-final — Close out

- **Skills:** none
- **What:** Add `Feature("x_admin_audit", "platform", "Audit trail viewer API: filters and the fleet-wide trail", "M4", Status.DONE, Layer.API, "", (), "<note>")`
  after `x_audit_log` in `scripts/spec.py`; run `uv run python scripts/cb.py docs-sync`;
  write `docs/contracts/x_admin_audit.md` (behaviour table from spec.md; parity
  table = "net-new, no v1"); update `HANDOFF.md` §4/§1 if relevant; note D2 in
  `docs/site/content/docs/feature-map.mdx` if it has a known-gaps list.
- **Where:** `scripts/spec.py`, `docs/contracts/x_admin_audit.md`, `HANDOFF.md`,
  `docs/site/content/docs/**` (docs-sync output),
  `.specs/features/x_admin_audit/tasks.md`
- **Depends on:** T3, T4
- **Reuses:** `docs/contracts/x_admin_api.md` as template
- **Done when:** `cb.py check` green.
- **Gate:** `uv run python scripts/cb.py check`
- **Commit:** `docs(x_admin_audit): close out`
