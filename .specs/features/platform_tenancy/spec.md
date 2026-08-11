# platform_tenancy — Specify

**Feature id:** `platform_tenancy` · **Milestone:** M1 · **Kind:** state report
**Status:** `partial` — schema, read path and per-tenant command enforcement
landed; `handler_pack`, `llm_overrides`, `storage_prefix` and the tenant cost
rollup still have no consumer.

This is not a build spec. It records what exists, what doesn't, and why —
per `scripts/status.py`'s rule that a `partial` needs a written reason, not a
plan.

## What is actually implemented today

- `tenants` reference table + `TenantRegistry` — `packages/cb-core/src/cb_core/tenancy.py:32-146`,
  migration `packages/cb-api/migrations/versions/0003_tenants.py`. L1-cached,
  invalidated over the existing pub/sub channel (`tenancy.py:136-143`).
- `groups.tenant_id` / `bots.tenant_id` columns, set at group upsert —
  `cb_core/groups.py:53,68,84,95,107,118`.
- **`feature_defaults` is actually wired into the config read path**, contrary
  to what the merge-order comment in `multi-tenant.mdx`'s rollout table might
  suggest ("next, with M1 config work" — that line is stale). `group_config.get_config`
  resolves the group's tenant from the same `groups` LEFT JOIN `group_configs`
  query and layers `tenant.feature_defaults` under `DEFAULTS` before the
  group's own row wins — `cb_core/group_config.py:186-213` (`_apply_tenant_defaults`,
  `_build_config`). Unit tested: `packages/cb-core/tests/test_group_config.py:65-104`.
- Two of v1's five personas are configured end to end: `cookiebot` and
  `bombot` — seeded as tenant rows (`0003_tenants.py:44-49`) and tokened in
  `.env.example:56`. `BotRegistry` (`cb_gateway/bots.py`) serves both from one
  process, which is the actual point of this feature: v1 ran one OS process
  per persona (`get_bot_token`, `is_alternate_bot`,
  `../COOKIEBOT-Telegram-Group-Bot/Bot/universal_funcs.py:39-52`,
  `COOKIEBOT.py:24-32`).
- `tenancy.registry.by_skin` is consulted in two places outside
  `group_config`, both sharing one predicate. `command_available_for_tenant`
  and the `command_catalog` fetch used to live only in `listcommand.py`; they
  now live in `cb_gateway/command_catalog.py:1-58` so both callers read the
  same rule instead of risking two copies drifting apart.
- **`disabled_commands` is enforced at dispatch, not just in `/commands`'
  listing.** `TenantCommandGateMiddleware` (`cb_gateway/middlewares.py`,
  registered last of the three outer middlewares in `cb_gateway/main.py`) runs
  on every update, reads the command `TelemetryMiddleware` already parsed into
  `data["parsed_command"]`, and drops the update — silently, matching how a
  v1 persona without a given handler simply produced no reply — when
  `command_available_for_tenant` says no. A plain message, join or callback
  with no command costs it nothing: the tenant/catalog lookups only run when
  there is a command to check. It fails open on a registry or catalog outage
  (same rule `DedupeMiddleware`'s cache branch follows), counts a drop under
  `metrics.updates_dropped_total{reason="tenant_disabled"}`, and logs
  `tenant_gate.command_disabled`. Unit tested end to end, including the
  fail-open path: `packages/cb-gateway/tests/test_tenant_command_gate.py`.

## What is missing

- **`handler_pack` is schema-only.** `Tenant.handler_pack` exists
  (`tenancy.py:37`) and every tenant row has a value, but nothing reads it.
  `build_router()` (`cb_gateway/handlers/__init__.py:39-76`) is one fixed
  router built once at startup for every tenant. The "packs compose the core
  router" mechanism `multi-tenant.mdx`'s "Custom implementations: handler
  packs" section describes is documented, not built — no `packs/` directory
  exists in the repo.
- **`llm_overrides` and `storage_prefix` are schema-only**, same as
  `handler_pack`: `cb_core/llm/router.py` never looks at a tenant when
  resolving a task's model, and `cb_core/storage.py` never applies a prefix
  to a key. Grepping the whole tree for either name outside `tenancy.py`
  itself and its own test/migration finds nothing.
- **`owner_ids` is unconsumed** — the feature that would read it,
  `x_owner_commands`, is still `Status.PLANNED`.
- **`tenant_monthly_cost` has no writer.** The table exists
  (`0003_tenants.py:82-96`, distributed and colocated with `groups`), but
  nothing rolls `llm_usage` up into it, so per-tenant budget enforcement
  (`monthly_llm_budget_usd`) has nothing to compare against even if the
  router did consult it.
- **Three of v1's five personas have no tenant row and no token**:
  `pawstralbot`, `tarinbot`, `connectbot` (`get_bot_token`,
  `universal_funcs.py:39-52`, cases `2`, `3`, `4`). Only `cookiebot` (`0`) and
  `bombot` (`1`) are configured.
- v1's `funfunctions or is_alternate_bot` behaviour (`COOKIEBOT.py:143` — an
  alternate-skin bot has fun features on regardless of the group's own
  config) has no v2 equivalent. `feature_defaults` is the mechanism that
  *could* express this, but no tenant row sets it that way, and nobody has
  decided whether it should.

## Why it stopped there

The schema and the read path landed as a **shared prerequisite** other M1
work needed (`group_config`, `core_setlang`), not as a feature being built
for its own sake — the same "build the plumbing once" order `platform_llm`
and `platform_storage` followed at M0. `disabled_commands` enforcement was the
one piece of the remainder concrete and small enough to close outside that
schedule — a predicate that already existed (`command_available_for_tenant`)
plus one outer middleware, no design work required. Everything else (handler
packs, budget enforcement, the owner model) is still explicitly scheduled for
M3, in `multi-tenant.mdx`'s own rollout table, *alongside* the feature that
would be its first real consumer (`x_custom_commands` for packs,
`x_owner_commands` for `owner_ids`). Nobody has built a pack because nothing
yet needs one badly enough to justify the design work — this is unscheduled,
not stuck.

## What it would take to finish, and what blocks it

Nothing here is blocked on an external dependency; it is unscheduled work:

1. At least one real handler pack, to prove the `build(core_router) -> Router`
   interface `multi-tenant.mdx` designs — `x_custom_commands` is the natural
   first consumer per the rollout table.
2. `llm_overrides` consulted in `LLMRouter.config_for`/`provider_for`, and
   `storage_prefix` applied in `cb_core/storage.py`'s key derivation.
3. A worker job rolling `llm_usage` into `tenant_monthly_cost`, before budget
   enforcement can mean anything.
4. Tenant rows + tokens for the 3 missing personas, if they're still wanted —
   that's a product question, not an engineering one.

## v1 equivalent

`get_bot_token()` / `is_alternate_bot` dispatch —
`../COOKIEBOT-Telegram-Group-Bot/Bot/universal_funcs.py:39-52`,
`../COOKIEBOT-Telegram-Group-Bot/Bot/COOKIEBOT.py:24-32,143`.
