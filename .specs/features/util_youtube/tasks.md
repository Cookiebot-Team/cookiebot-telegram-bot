# util_youtube — Tasks

## Status

| Task | Status | Notes |
|------|--------|-------|
| T1 — Job-name constant + settings | ✅ done | |
| T2 — Worker job | ✅ done | |
| T3 — Gateway handler + registration | ✅ done | |
| T4 — Unit tests | ✅ done | |
| T5 — Acceptance | ✅ done | |
| T-final — Close out | ✅ done | |

## T1 — Job-name constant + settings

- **What:** `YOUTUBE_SEARCH = "youtube_search"` in `cb_core/jobs.py`.
  `youtube_api_key`/`youtube_timeout_seconds` in `cb_core/settings.py` and
  `.env.example`.
- **Where:** `packages/cb-core/src/cb_core/jobs.py`,
  `packages/cb-core/src/cb_core/settings.py`, `.env.example`
- **Depends on:** none
- **Gate:** `uv run ruff check packages/cb-core/src/cb_core/jobs.py packages/cb-core/src/cb_core/settings.py`
- **Commit:** folded into T2

## T2 — Worker job

- **What:** `search_youtube` per design R1.2/R2/R3/R4.
- **Where:** `packages/cb-worker/src/cb_worker/jobs/youtube.py` (new),
  `packages/cb-worker/src/cb_worker/main.py` (registration)
- **Depends on:** T1
- **Gate:** `uv run mypy packages/cb-worker/src/cb_worker/jobs/youtube.py`
- **Commit:** `feat(util_youtube): the search + reply, off the reply path`

## T3 — Gateway handler + registration

- **What:** `youtube` handler per design R1.1.
- **Where:** `packages/cb-gateway/src/cb_gateway/handlers/youtube.py` (new),
  `packages/cb-gateway/src/cb_gateway/handlers/__init__.py` (one router line)
- **Depends on:** T1
- **Gate:** `uv run mypy packages/cb-gateway/src/cb_gateway/handlers/youtube.py`
- **Commit:** folded into T2's commit

## T4 — Unit tests

- **What:** Job: empty/failed/timeout/malformed response all degrade to
  `not_found`; a real response picks a random video (seeded rng); reaction
  suppressed on failure. Handler: no-query → `youtube_need`, gate refusal,
  enqueue payload shape.
- **Where:** `packages/cb-worker/tests/test_youtube_job.py` (new),
  `packages/cb-gateway/tests/test_youtube.py` (new)
- **Depends on:** T2, T3
- **Gate:** `uv run pytest packages/cb-worker/tests/test_youtube_job.py packages/cb-gateway/tests/test_youtube.py -q`
- **Commit:** folded into T2's commit

## T5 — Acceptance

- **What:** Copy `../Cookiebot-QA/features/util_youtube.feature` verbatim.
  Mock the HTTP call (AGENTS.md §6 — YouTube is the outside world), assert
  the enqueue + the worker's eventual reply via a fake queue that runs the
  job inline, mirroring `qa/test_util_everyone.py`'s fake-queue pattern.
- **Where:** `qa/features/util_youtube.feature` (new), `qa/test_util_youtube.py` (new)
- **Depends on:** T3, T4
- **Gate:** `uv run pytest qa/test_util_youtube.py -q`
- **Commit:** `test(util_youtube): the QA scenario`

## T-final — Close out

- **What:** `docs/contracts/util_youtube.md`, `scripts/spec.py` → `done`,
  `cb.py docs-sync`, `.mdx` prose, `HANDOFF.md` update.
- **Depends on:** T1-T5
- **Gate:** `uv run python scripts/cb.py check`
- **Commit:** `docs(util_youtube): close out`
