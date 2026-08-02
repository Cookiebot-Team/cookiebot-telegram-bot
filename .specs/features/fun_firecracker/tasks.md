# fun_firecracker — Tasks

Grammar: `tlc-spec-driven` §5. Read `spec.md` and `design.md` first.

## Status

| Task | Status | Notes |
|------|--------|-------|
| T1 — Failing tests for the burst and the triggers | ✅ done | bottom of the pyramid, red first |
| T2 — Handler and router registration | ✅ done | turns T1 green |
| T3 — Acceptance scenarios | ✅ done | includes the net-new fun-off scenario |
| T-final — Close out | ✅ done | contract, spec.py flip, docs-sync |

## Tasks

### T1 — Failing tests for the burst and the triggers

- **Skills:** /migrate-feature (Phase 4)
- **What:** Write the unit tests before the handler exists. Cover (a) every
  trigger — `/rojao /rojão /acende /fogos /firecracker`, each bare, each with a
  trailing argument, each with `@botname` — resolving through the existing
  `COMMAND_ALIASES` entry to canonical `firecracker`; (b) the pure `burst()`
  invariants from design R2.2: total `pra` count equals the drawn `amount`,
  `5 <= amount <= 20`, every element is `"pra " * k` with `k >= 1`, list
  non-empty, checked over a seeded `random.Random` and over 1000 seeds.
- **Where:** `packages/cb-gateway/tests/test_firecracker.py` (new).
- **Depends on:** none
- **Reuses:** `cb_core/textmatch.py:47-48` (aliases already present — do not add
  any); the assertion style of `packages/cb-gateway/tests/test_ship.py`.
- **Done when:** the file exists, the trigger tests pass against the existing
  textmatch table, and the `burst()` tests fail on import because
  `cb_gateway.handlers.firecracker` does not exist yet.
- **Gate:** `uv run pytest packages/cb-gateway/tests/test_firecracker.py -q`
  (expected: trigger tests pass, burst tests error on import)
- **Commit:** `test(fun_firecracker): the burst maths and every v1 trigger`
- **→ R2.2, R5.1**

### T2 — Handler and router registration

- **Skills:** /migrate-feature (Phase 5)
- **What:** Implement the handler per design R1–R4. Pure `burst(rng)` builder,
  then the sequence: react `🎉` (suppressed) → reply `"fiiiiiiii.... "` →
  `await asyncio.sleep(0.1)` → one send per burst line → send
  `"<b> 💥POOOOOOOWW💥 </b>"`. Fun-gate first: disabled ⇒ single `fun_off` reply
  and `mark_outcome`, nothing else. The three literals are module constants,
  each commented with its `Miscellaneous.py` line and the D-FC-1 note that they
  are deliberately unlocalised.
- **Where:** `packages/cb-gateway/src/cb_gateway/handlers/firecracker.py` (new);
  `packages/cb-gateway/src/cb_gateway/handlers/__init__.py` (import + one
  `include_router` line in the disjoint-commands block, next to `ship`/`dice` —
  **not** in the join chain).
- **Depends on:** T1
- **Reuses:** `handlers/ship.py` for the whole shape — its fun-gate call, its
  `contextlib.suppress(Exception)` reaction, its `mark_outcome` call and its
  rng idiom. `cb_gateway/context.py` (`context_for`, `t`),
  `cb_gateway/filters.py` (`CommandName`).
- **Done when:** T1 is fully green and `handlers/__init__.py` registers the
  router exactly once.
- **Gate:** `uv run pytest packages/cb-gateway/tests/test_firecracker.py -q && uv run python scripts/cb.py types`
- **Commit:** `feat(fun_firecracker): the fuse, the burst and the bang`
- **→ R1.1, R1.2, R1.4, R3, R4**

### T3 — Acceptance scenarios

- **Skills:** /migrate-feature (Phase 3 + Phase 4 top layer)
- **What:** Copy `../Cookiebot-QA/features/fun_firecracker.feature` into
  `qa/features/` **wording unchanged**, then append one net-new scenario for the
  fun-off gate (v1 replies with `fun_off`, it does not go silent). Drive both
  from a step file against the mock Telegram API: assert the first bot message
  is exactly `fiiiiiiii.... `, that at least one message matches `^(pra )+$`,
  and that the last is exactly `<b> 💥POOOOOOOWW💥 </b>`.
- **Where:** `qa/features/fun_firecracker.feature` (new),
  `qa/test_fun_firecracker.py` (new).
- **Depends on:** T2
- **Reuses:** `qa/test_fun_ship.py` for the pytest-bdd wiring; `qa/conftest.py`
  fixtures; `next_update_id()` for every update (a reused id is dropped by the
  dedupe middleware and reads as "the bot said nothing").
- **Done when:** both scenarios pass and no existing acceptance test regressed.
- **Gate:** `uv run pytest qa/test_fun_firecracker.py -q`
- **Commit:** `test(fun_firecracker): the QA scenario plus the gate-off case`
- **→ R5.3**

### T-final — Close out

- **Skills:** none
- **What:** Write `docs/contracts/fun_firecracker.md` with the Phase-2 table
  from `spec.md` (v1 `file:line` intact) and a Phase-6 parity table stating, per
  row, identical-or-drift; record D-FC-1 (unlocalised, preserved), D-FC-2
  (flood risk, preserved) and R5.2 (no integration test, and why). Flip
  `fun_firecracker` to `done` in `scripts/spec.py`, run `cb.py docs-sync`, and
  mark the row done in this file's Status table.
- **Where:** `docs/contracts/fun_firecracker.md` (new), `scripts/spec.py`,
  regenerated `docs/site/**` output, `.specs/features/fun_firecracker/tasks.md`.
- **Depends on:** T3
- **Reuses:** `docs/contracts/fun_ship.md` as the format.
- **Done when:** `cb.py check` exits 0 — that runs `status --check` and
  `docs-sync --check`, which fail if a feature claims done without a ported
  passing scenario or if frontmatter drifted.
- **Gate:** `uv run python scripts/cb.py check`
- **Commit:** `docs(fun_firecracker): close out`
- **→ R6**
