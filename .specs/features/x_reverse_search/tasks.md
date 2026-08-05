# x_reverse_search — Tasks

## Status

| Task | Status | Notes |
|------|--------|-------|
| T1 — Aliases, settings, job name | ⏳ not started | three triggers, none aliased yet |
| T2 — Worker job | ⏳ not started | the SauceNAO call, D-RS-1's fix |
| T3 — Gateway handler + registration | ⏳ not started | |
| T4 [P] — Unit tests | ⏳ not started | |
| T5 — Acceptance | ⏳ not started | authored, no QA scenario exists |
| T-final — Close out | ⏳ not started | needs a new feature-map row |

### T1 — Aliases, settings, job name

- **Skills:** /migrate-feature
- **What:** `buscarfonte`/`searchsource`/`buscarfuente` → canonical
  `searchsource` in `COMMAND_ALIASES` (AGENTS.md §2.1 — all three are live v1
  triggers). `saucenao_api_key`, `saucenao_timeout_seconds` (15.0) in settings
  and `.env.example`. `REVERSE_SEARCH = "reverse_search"` in `cb_core/jobs.py`.
- **Where:** `packages/cb-core/src/cb_core/{textmatch,settings,jobs}.py`, `.env.example`
- **Depends on:** none
- **Gate:** `uv run ruff check packages/cb-core/src`
- **Commit:** folded into T2

### T2 — Worker job

- **What:** `search_source` per design R2-R5: download via `bot.download`,
  multipart POST to SauceNAO, the two rate-limit branches off the response
  header, the `> 80` threshold on `results[0]` only, v1's exact answer string,
  the two reactions, and every other failure degrading to `reverse_no_found`.
- **Where:** `packages/cb-worker/src/cb_worker/jobs/reverse_search.py` (new),
  `cb_worker/main.py` (registration)
- **Depends on:** T1
- **Reuses:** `cb_worker/jobs/youtube.py`'s job wrapper and `set_http_client` seam
- **Gate:** `uv run mypy packages/cb-worker/src`
- **Commit:** `feat(x_reverse_search): find an image's source without leaking the bot token`

### T3 — Gateway handler + registration

- **What:** the `utility` gate, the reply check, `file_id` resolution
  (D-RS-5), the enqueue. Registered in the command block.
- **Where:** `packages/cb-gateway/src/cb_gateway/handlers/reverse_search.py` (new),
  `handlers/__init__.py`
- **Depends on:** T1
- **Gate:** `uv run mypy packages/cb-gateway/src`
- **Commit:** folded into T2's commit

### T4 [P] — Unit tests

- **What:** all three aliases resolve. Job: hit above/at/below the threshold
  (81 vs 80 — v1's `>` is strict), author present and absent, `ext_urls`
  empty, short-limit, long-limit, non-2xx, timeout, malformed JSON, no key.
  **And that the request carries a file part and no `url` parameter** —
  D-RS-1's regression test. Handler: no reply, a reply with no image, gate
  off, the enqueue payload.
- **Where:** `packages/cb-worker/tests/test_reverse_search_job.py`,
  `packages/cb-gateway/tests/test_reverse_search.py`
- **Depends on:** T2, T3
- **Gate:** `uv run pytest packages/cb-worker/tests/test_reverse_search_job.py packages/cb-gateway/tests/test_reverse_search.py -q`
- **Commit:** folded into T2's commit

### T5 — Acceptance

- **What:** `qa/features/x_reverse_search.feature`, **authored** — no QA
  scenario exists. Cover: the no-reply refusal, a found source, no match, and
  the daily-limit message. SauceNAO mocked (the outside world); the job run
  inline through the fake-queue pattern.
- **Where:** `qa/features/x_reverse_search.feature`, `qa/test_x_reverse_search.py`
- **Depends on:** T3, T4
- **Gate:** `uv run pytest qa/test_x_reverse_search.py -q`
- **Commit:** `test(x_reverse_search): the scenario v1 never had`

### T-final — Close out

- **What:** `docs/contracts/x_reverse_search.md`; `scripts/spec.py` → `done`;
  `cb.py docs-sync`; **a new `feature-map.mdx` row** (the map has none) plus a
  D-item for the token leak; `HANDOFF.md`.
- **Depends on:** T1-T5
- **Gate:** `uv run python scripts/cb.py check`
- **Commit:** `docs(x_reverse_search): close out`
