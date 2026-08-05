# util_postgetter — Tasks

Depends on `util_postforwarder`'s T1–T3 and T6 for the table, the pending-post
cache, the shared media resolver and the callback wire.

## Status

| Task | Status | Notes |
|------|--------|-------|
| T1 — The `publisher_ask` prompt handler | ⏳ not started | |
| T2 [P] — Unit tests | ⏳ not started | |
| T3 — Acceptance | ⏳ not started | |
| T-final — Close out | ⏳ not started | |

### T1 — The `publisher_ask` prompt handler

- **Skills:** /migrate-feature
- **What:** `handlers/postgetter.py` per design R1: the six-way filter plus
  `from_user.first_name == "Telegram"` (R1.1), the `publisher_ask` gate that
  raises `SkipHandler` when off (R1.2), the pt/en-only prompt text (R1.3,
  `es` falls back by omission), the ✔️/❌ keyboard built by
  `publisher.build_approval_request` (R1.4, the ❌ payload is the bare `nPub`),
  the `pending_posts.put` write (R1.5), and registration immediately before
  `fun_random.router` (R1.6). Telemetry per R3.1.
- **Where:** `packages/cb-gateway/src/cb_gateway/handlers/postgetter.py` (new),
  `packages/cb-gateway/src/cb_gateway/handlers/__init__.py` (one router line
  plus the comment explaining the `elif` order)
- **Depends on:** `util_postforwarder` T3, T6
- **Reuses:** `cb_core.publisher.resolve_pending_media`,
  `cb_core.pending_posts`, `cb_gateway.handlers.publisher.build_approval_request`
- **Done when:** an auto-forwarded channel post with a caption is prompted and
  **not** pooled by `fun_random`; the same post with `publisher_ask` off falls
  through.
- **Gate:** `uv run mypy packages/cb-gateway/src/cb_gateway/handlers/postgetter.py`
- **Commit:** `feat(util_postgetter): offer to share an auto-forwarded channel ad`
- **→ R1, R3**

### T2 [P] — Unit tests

- **Skills:** /migrate-feature
- **What:** The filter's six conditions, one negative case each; the
  `first_name` discriminator; `publisher_ask` off → `SkipHandler`; the exact
  prompt text for `en`/`pt`/`es` (proving D-PG-3, that `es` gets English); the
  callback payload shape including the bare `nPub`; the cache key and the
  document→`animation` mapping.
- **Where:** `packages/cb-gateway/tests/test_postgetter.py` (new)
- **Depends on:** T1
- **Gate:** `uv run pytest packages/cb-gateway/tests/test_postgetter.py -q`
- **Commit:** folded into T1's commit
- **→ R1**

### T3 — Acceptance

- **Skills:** /migrate-feature
- **What:** `qa/features/util_postgetter.feature` — the QA scenario ported
  wording-unchanged, asserting the forward attribution and the surviving inline
  keyboard, plus an authored scenario for the `publisher_ask` prompt itself
  (spec.md's conflict note: QA has none). Drives the real dispatcher.
- **Where:** `qa/features/util_postgetter.feature` (new),
  `qa/test_util_postgetter.py` (new)
- **Depends on:** T1, T2, `util_postforwarder` T5
- **Gate:** `uv run pytest qa/test_util_postgetter.py -q`
- **Commit:** `test(util_postgetter): the prompt and the delivery gate`
- **→ R1, R2**

### T-final — Close out

- **Skills:** /migrate-feature, /review-changes, /lint-code
- **What:** `docs/contracts/util_postgetter.md` with the Phase-2 and Phase-6
  tables and D-PG-1..4's verdicts; `scripts/spec.py` → `done`;
  `cb.py docs-sync`; the `.mdx` prose; the QA conflict in `feature-map.mdx`.
- **Where:** `docs/contracts/util_postgetter.md`, `scripts/spec.py`,
  `docs/site/content/docs/feature-map.mdx`
- **Depends on:** T1–T3
- **Gate:** `uv run python scripts/cb.py check`
- **Commit:** `docs(util_postgetter): close out`
