# x_custom_commands — Tasks

Grammar: `tlc-spec-driven` §5. Read `spec.md` (the blocker, now cleared, and
the v1 contract) and `design.md` (R1-R3) first.

## Status

| Task | Status | Notes |
|------|--------|-------|
| T1 — `CustomCommandName` filter | ✅ done | R1 |
| T2 — Handler pack registry | ✅ done | R3; also closes platform_tenancy |
| T3 — Handler and router registration | ✅ done | R2 |
| T4 — Acceptance scenarios | ✅ done | authored locally; QA could not have one |
| T-final — Close out | ✅ done | spec rows BLOCKED → DONE, PARTIAL → DONE |

## Tasks

### T1 — `CustomCommandName` filter

- **Skills:** /migrate-feature
- **What:** Head extraction per R1.2, membership against
  `legacy_assets.entries_for_custom`, injecting `(name, args)`.
- **Where:** `packages/cb-gateway/src/cb_gateway/filters.py`,
  `packages/cb-gateway/tests/test_custom_command.py`
- **Depends on:** the `legacy-catalog` generation commit
- **Reuses:** `parse_command`'s rules
- **Done when:** `/louie@ThisBot 2` matches and `/louie@OtherBot` does not.
- **Gate:** `uv run pytest packages/cb-gateway/tests/test_custom_command.py -q`
- **Commit:** folded into T3's commit
- **→ R1.1-R1.4**

### T2 — Handler pack registry

- **Skills:** /implement-feature
- **What:** `packs.py` per R3 — families, the `PACKS` map, `PackProvides`, the
  fail-open lookup. Update `multi-tenant.mdx` to describe what shipped and
  `docs/contracts/core_botskins.md`'s "still open" entry.
- **Where:** `packages/cb-gateway/src/cb_gateway/packs.py`,
  `docs/site/content/docs/multi-tenant.mdx`, `docs/contracts/core_botskins.md`
- **Depends on:** none
- **Reuses:** `cb_core.tenancy.registry`
- **Done when:** a tenant on `minimal` does not get the family and a registry
  outage still does.
- **Gate:** `uv run pytest packages/cb-gateway/tests/test_custom_command.py -q`
- **Commit:** folded into T3's commit
- **→ R3.1-R3.3**

### T3 — Handler and router registration

- **Skills:** /migrate-feature
- **What:** The handler per R2, registered last among the command routers.
- **Where:** `packages/cb-gateway/src/cb_gateway/handlers/custom_command.py`,
  `packages/cb-gateway/src/cb_gateway/handlers/__init__.py`
- **Depends on:** T1, T2
- **Reuses:** `death.py`'s storage read, `deny_if_disabled`
- **Done when:** `/louie` and `/louie 1` both answer with the right picture.
- **Gate:** `uv run pytest packages/cb-gateway/tests/test_custom_command.py -q`
- **Commit:** `feat(x_custom_commands): 53 pools, and the pack that gates them`
- **→ R2.1-R2.3**

### T4 — Acceptance scenarios

- **Skills:** /implement-feature (Phase 5)
- **What:** Random draw, indexed pick, out-of-range index, unknown name, the
  fun gate, and a brand on the `minimal` pack.
- **Where:** `qa/features/x_custom_commands.feature`,
  `qa/test_x_custom_commands.py`
- **Depends on:** T3
- **Done when:** all six scenarios pass.
- **Gate:** `uv run pytest qa/test_x_custom_commands.py -q`
- **Commit:** folded into T3's commit
- **→ R2.2, R3.1**

### T-final — Close out

- **Skills:** none
- **What:** `docs/contracts/x_custom_commands.md`; flip `x_custom_commands` and
  `platform_tenancy` in `scripts/spec.py`; `cb.py docs-sync`; feature-page
  prose; `HANDOFF.md`.
- **Where:** `docs/contracts/x_custom_commands.md`, `scripts/spec.py`,
  `docs/site/content/docs/features/x_custom_commands.mdx`, `HANDOFF.md`, this
  file
- **Depends on:** T4
- **Done when:** `uv run python scripts/cb.py check` exits 0.
- **Gate:** `uv run python scripts/cb.py check`
- **Commit:** folded into T3's commit
- **→ R1-R3**
