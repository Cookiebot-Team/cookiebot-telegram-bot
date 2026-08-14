# x_image_search — Tasks

Grammar: `tlc-spec-driven` §5. Read `spec.md` (five defects, five verdicts) and
`design.md` (R1-R5, especially R2.2) first.

## Status

| Task | Status | Notes |
|------|--------|-------|
| T1 — Blocklist and term extraction in cb-core | ✅ done | R4 |
| T2 — Worker job | ✅ done | R1.2, R5 |
| T3 — Handler, quota and dispatch | ✅ done | R2, R3 |
| T4 — Acceptance scenarios | ✅ done | authored locally; QA has none |
| T-final — Close out | ✅ done | spec row PLANNED → DONE |

## Tasks

### T1 — Blocklist and term extraction in cb-core

- **Skills:** /migrate-feature
- **What:** Vendor `Static/avoid_search.txt` byte-for-byte as package data,
  and port `search_term`/`is_avoided` with their warts (R4).
- **Where:** `packages/cb-core/src/cb_core/image_search.py`,
  `packages/cb-core/src/cb_core/asset_data/search/`,
  `packages/cb-core/pyproject.toml`,
  `packages/cb-core/tests/test_image_search.py`
- **Depends on:** none
- **Done when:** the 49 entries load from the installed package and
  `search_term("/cat @dog") == " cat "`.
- **Gate:** `uv run pytest packages/cb-core/tests/test_image_search.py -q`
- **Commit:** folded into T3's commit
- **→ R4.1-R4.3**

### T2 — Worker job

- **Skills:** /migrate-feature
- **What:** The Custom Search request and v1's shuffle-and-try send loop
  (R1.2, R5), with `youtube.py`'s job wrapper, metric and test seam.
- **Where:** `packages/cb-worker/src/cb_worker/jobs/image_search.py`,
  `packages/cb-worker/src/cb_worker/main.py`,
  `packages/cb-core/src/cb_core/jobs.py`,
  `packages/cb-core/src/cb_core/settings.py`,
  `packages/cb-worker/tests/test_image_search_job.py`
- **Depends on:** none
- **Reuses:** `cb_worker/jobs/youtube.py` wholesale
- **Done when:** a mocked Google response produces one `sendPhoto`, and a
  failing result falls through to the next.
- **Gate:** `uv run pytest packages/cb-worker/tests/test_image_search_job.py -q`
- **Commit:** folded into T3's commit
- **→ R1.2, R5.1**

### T3 — Handler, quota and dispatch

- **Skills:** /migrate-feature
- **What:** The prompt handler, the catch-all, the two guards, the shared
  quota (R3) and — the risky part — `SkipHandler` on both non-matches (R2.2).
- **Where:** `packages/cb-gateway/src/cb_gateway/handlers/image_search.py`,
  `packages/cb-gateway/src/cb_gateway/handlers/__init__.py`,
  `packages/cb-core/src/cb_core/textmatch.py`,
  `packages/cb-gateway/tests/test_image_search.py`
- **Depends on:** T1, T2
- **Reuses:** `cache.incr_window`, `youtube.py`'s enqueue shape
- **Done when:** `/french fries` queues a search and `/random` still works.
- **Gate:** `uv run pytest packages/cb-gateway/tests/test_image_search.py qa -q`
- **Commit:** `feat(x_image_search): v1's catch-all, metered and off the reply path`
- **→ R2.1-R3.4**

### T4 — Acceptance scenarios

- **Skills:** /implement-feature (Phase 5)
- **What:** The prompt, a search, both safe-search settings, the blocklist, a
  pasted link, another bot's command, the daily limit, utility off — and "a
  real command is never turned into a search".
- **Where:** `qa/features/x_image_search.feature`, `qa/test_x_image_search.py`
- **Depends on:** T3
- **Done when:** all ten scenarios pass and the rest of `qa/` is unchanged.
- **Gate:** `uv run pytest qa -q`
- **Commit:** folded into T3's commit
- **→ R2.2, R3.1, R5.1**

### T-final — Close out

- **Skills:** none
- **What:** `docs/contracts/x_image_search.md`; flip `scripts/spec.py`;
  `cb.py docs-sync`; feature-page prose; `.env.example` for the two new
  credentials; `HANDOFF.md`.
- **Where:** `docs/contracts/x_image_search.md`, `scripts/spec.py`,
  `docs/site/content/docs/features/x_image_search.mdx`, `.env.example`,
  `HANDOFF.md`, this file
- **Depends on:** T4
- **Done when:** `uv run python scripts/cb.py check` exits 0.
- **Gate:** `uv run python scripts/cb.py check`
- **Commit:** folded into T3's commit
- **→ R1-R5**
