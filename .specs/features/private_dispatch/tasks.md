# private_dispatch — Tasks

Grammar: `tlc-spec-driven` §5. Read `spec.md` and `design.md` first.

## Status

| Task | Status | Notes |
|------|--------|-------|
| T1 — `PrivateContext` + unit tests | ✅ done | |
| T2 — Retrofit `privacy.py` (the live bug fix) | ✅ done | |
| T3 — Retrofit `listcommand.py` (relocate the ad hoc branch) | ✅ done | |
| T4 — Acceptance: `/privacy` in a DM | ✅ done | |
| T-final — Close out | ✅ done | |

## Tasks

### T1 — `PrivateContext` + unit tests

- **Skills:** /implement-feature (new infrastructure, no v1 equivalent to port)
- **What:** `packages/cb-gateway/src/cb_gateway/private_context.py` per
  design R1: `PrivateContext(user_id: int)`, `private_context_for(message) ->
  PrivateContext`, synchronous, no `group_id` field. Docstring states R1.2's
  "no `await` because there is nothing to query" reasoning directly — this is
  the load-bearing design decision, not an implementation detail to leave
  implicit.
- **Where:** `packages/cb-gateway/src/cb_gateway/private_context.py` (new),
  `packages/cb-gateway/tests/test_private_context.py` (new)
- **Depends on:** none
- **Done when:** `private_context_for` correctly reads `user_id` off a
  private-chat-shaped `Message`.
- **Gate:** `uv run pytest packages/cb-gateway/tests/test_private_context.py -q && uv run python scripts/cb.py types`
- **Commit:** `feat(cb-gateway): a context for private chats that cannot touch group_id`
- **→ R1**

### T2 — Retrofit `privacy.py` (the live bug fix)

- **Skills:** /migrate-feature (Phase 5 — this is a v1 behaviour, `COOKIEBOT.py:87-88`)
- **What:** Split the single handler into `privacy_private`
  (`F.chat.type == ChatType.PRIVATE`, hardcoded `locales.get("privacy", "en")`)
  and `privacy` (`F.chat.type != ChatType.PRIVATE`, unchanged group behaviour)
  per design R2.1.
- **Where:** `packages/cb-gateway/src/cb_gateway/handlers/privacy.py`,
  `packages/cb-gateway/tests/test_privacy.py` (new — no unit tests existed
  for this handler before; add one confirming the group handler's decorator
  now excludes private chats, so `T4`'s acceptance scenario is exercising
  the intended code path, not a filter that happens to overlap)
- **Depends on:** T1 (imports `private_context` — not called from this
  handler per design R2.1, but the module is where the router-split pattern
  is documented; import kept for readers, not removed just because this one
  retrofit ends up not needing `.user_id`) — actually: **no hard dependency**,
  T2 can run in parallel with T1 since it does not call `private_context_for`.
  Listed as depending on T1 only for commit ordering (the mechanism's module
  docstring is the natural place a reviewer looks first).
- **Gate:** `uv run pytest packages/cb-gateway/tests/test_privacy.py -q && uv run python scripts/cb.py types`
- **Commit:** `fix(core_privacy): answer /privacy in a DM instead of querying a fake group`
- **→ R2.1**

### T3 — Retrofit `listcommand.py` (relocate the ad hoc branch)

- **Skills:** /migrate-feature (Phase 5 — still v1 behaviour, now via the
  shared pattern instead of an inline branch)
- **What:** Split per design R2.2. Pure relocation — no behaviour change,
  confirmed by the existing acceptance scenario (`qa/features/core_listcommand.feature`'s
  "User types /commands in a private chat with the bot") staying green
  unmodified.
- **Where:** `packages/cb-gateway/src/cb_gateway/handlers/listcommand.py`
- **Depends on:** none (parallel with T1/T2 — touches a different file, no
  shared state)
- **Done when:** `qa/test_core_listcommand.py` passes unmodified.
- **Gate:** `uv run pytest qa/test_core_listcommand.py -q && uv run python scripts/cb.py types`
- **Commit:** `refactor(core_listcommand): the private-chat branch through the shared pattern`
- **→ R2.2**

### T4 — Acceptance: `/privacy` in a DM

- **Skills:** /migrate-feature (Phase 3 + top of Phase 4)
- **What:** Add a net-new scenario to `qa/features/core_privacy.feature`
  (upstream QA has none for this — spec.md's QA section) plus a
  `make_private_message_update` helper in `qa/conftest.py`, promoted out of
  `qa/test_core_listcommand.py`'s local `_make_private_update` (same shape,
  now shared — this slice owns `qa/conftest.py` since it is building the
  shared private-chat mechanism). Update `qa/test_core_listcommand.py` to use
  the promoted helper instead of its own copy.
- **Where:** `qa/features/core_privacy.feature`, `qa/test_core_privacy.py`,
  `qa/conftest.py` (new `make_private_message_update`),
  `qa/test_core_listcommand.py` (use the promoted helper)
- **Depends on:** T2
- **Done when:** the new scenario passes and `qa/test_core_listcommand.py`
  still passes unmodified in behaviour after the helper swap.
- **Gate:** `uv run pytest qa/test_core_privacy.py qa/test_core_listcommand.py -q`
- **Commit:** `test(core_privacy): the DM scenario, and a shared private-update builder`
- **→ QA section (spec.md)**

### T-final — Close out

- **Skills:** none
- **What:** Update `docs/contracts/core_privacy.md`'s Phase 6 table (the
  "Language selection (private chat...)" row currently says "not ported" —
  flip it) and its Phase 2 table (add the DM row). Update
  `docs/contracts/core_listcommand.md` to note the implementation moved to
  the shared pattern (no behaviour-verdict change). `HANDOFF.md` §1: mark gap
  2 closed, note `/start`/owner-ops as the remaining named follow-ups.
  `cb.py docs-sync` if either feature's `.mdx` frontmatter needs regenerating
  (neither's `status`/`notes` in `scripts/spec.py` actually changes — both
  were already `done` — so this may be a no-op; verify rather than assume).
- **Where:** `docs/contracts/core_privacy.md`, `docs/contracts/core_listcommand.md`,
  `HANDOFF.md`, this file's Status table
- **Depends on:** T1-T4
- **Done when:** `uv run python scripts/cb.py check` exits 0.
- **Gate:** `uv run python scripts/cb.py check`
- **Commit:** `docs(private_dispatch): close out`
