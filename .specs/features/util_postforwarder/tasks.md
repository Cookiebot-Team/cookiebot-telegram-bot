# util_postforwarder — Tasks

Execution note: this slice ships three features off one v1 file. The shared
pieces (T1–T3) are prerequisites for `util_postgetter` and
`util_deletereposts` as well as for everything below them here.

## Status

| Task | Status | Notes |
|------|--------|-------|
| T1 — Migration `0005` + `scheduled_posts` repository | ⏳ not started | shared with the other two features |
| T2 — Settings, job names, locale strings, `translate` task | ⏳ not started | shared |
| T3 — `cb_core/publisher.py` + `cb_core/pending_posts.py` | ⏳ not started | shared pure logic |
| T4 — Worker: `publisher_approve` (render + fan-out) | ⏳ not started | |
| T5 — Worker: `deliver_scheduled_posts` cron | ⏳ not started | |
| T6 — Gateway: `/divulgar`, `/repost`, callbacks, reply relay | ⏳ not started | |
| T7 [P] — Unit tests | ⏳ not started | |
| T8 [P] — Integration tests | ⏳ not started | needs Citus |
| T9 — Acceptance | ⏳ not started | |
| T-final — Close out | ⏳ not started | |

### T1 — Migration `0005` + `scheduled_posts` repository

- **Skills:** /migrate-feature
- **What:** The table exactly as design R1.1 spells it, created with raw SQL in
  `op.execute` so the shard key and colocation are visible in the diff, plus
  the three indexes of R1.2. `downgrade()` drops it. Then a repository module
  with the statements every consumer needs: `create`, `due_before`,
  `delete`, `delete_by_requester`, `count_for_group`, `trim_to_max`,
  `delete_by_origin_title`, `find_by_origin_title`, `advance_or_expire`.
  UUIDv7 via `cb_core.ids.uuid7` (AGENTS.md §2.3). Every single-group statement
  filters on `group_id`; the two that cannot (`delete_by_requester`,
  `find_by_origin_title`) carry design R1.4's comment.
- **Where:** `packages/cb-api/migrations/versions/0005_scheduled_posts.py` (new),
  `packages/cb-core/src/cb_core/scheduled_posts.py` (new)
- **Depends on:** none
- **Reuses:** `cb_core.db`, `cb_core.ids.uuid7`, migration `0004`'s shape
- **Done when:** `cb.py migrate-check` runs upgrade → downgrade → upgrade green.
- **Gate:** `uv run python scripts/cb.py migrate-check`
- **Commit:** `feat(util_postforwarder): the schedule table v1 kept in a local SQLite file`
- **→ R1.1, R1.2, R1.3, R1.4**

### T2 — Settings, job names, locale strings, `translate` task

- **Skills:** /migrate-feature
- **What:** `postmail_chat_id`, `postmail_chat_link`, `approval_chat_id`,
  `publisher_hidden_author_names` (default `("Mekhy",)`, D-PF-10),
  `publisher_pending_ttl_seconds` (86400), `exchangerate_api_key`,
  `exchangerate_timeout_seconds` (10.0) in `cb_core/settings.py` and
  `.env.example`. `PUBLISHER_APPROVE = "publisher_approve"` in
  `cb_core/jobs.py`. The twelve `cb.json` keys from `spec.md`'s string table,
  in `en`/`pt`/`es` — **never** `lib.json`. A `translate` entry in
  `DEFAULT_TASKS` per design R5.1. `price-parser` added to `cb-core`'s
  dependencies (R5.4).
- **Where:** `packages/cb-core/src/cb_core/settings.py`,
  `packages/cb-core/src/cb_core/jobs.py`,
  `packages/cb-core/src/cb_core/llm/router.py`,
  `packages/cb-core/src/cb_core/locale_data/{en,pt,es}/cb.json`,
  `packages/cb-core/pyproject.toml`, `.env.example`
- **Depends on:** none
- **Reuses:** `x_speech_to_text`'s precedent for adding `cb.json` keys and a task
- **Done when:** `locales.get(k, lang)` returns the spec's exact string for
  every key × language, and `router().config_for("translate")` resolves.
- **Gate:** `uv run ruff check packages/cb-core && uv run mypy packages/cb-core`
- **Commit:** folded into T3
- **→ R5.1, R5.4, R5.5**

### T3 — `cb_core/publisher.py` + `cb_core/pending_posts.py`

- **Skills:** /migrate-feature
- **What:** The pure logic both the gateway and the worker need, so neither
  imports the other (R3.2). `publisher.py`: `resolve_pending_media(message)`
  (v1 `add_post_to_cache`, document→`animation`, D-PF-4),
  `emojis_to_numbers` (`universal_funcs.py:353-356`),
  `remove_emojis_from_ends` (`:175-180`), `extract_caption_urls`
  (v1's `URL_REGEX`, `:23`), `build_post_keyboard` (the five-step order of
  `prepare_post :184-199`, including the `< 5 rows` entity cap and the
  hidden-author-name check), `convert_prices_in_text` (`:129-173`, D-PF-6
  preserved, rates injected so it is testable and memoised), and
  `finalise_caption` (`<`→`⩽`, `>`→`⩾`, `&`→`＆`, 1020-char truncation).
  `pending_posts.py`: design R2 — `PendingPost` msgspec struct, `put`/`get`/
  `take` over `cb_core.cache` with the R2.2 keyspace.
- **Where:** `packages/cb-core/src/cb_core/publisher.py` (new),
  `packages/cb-core/src/cb_core/pending_posts.py` (new)
- **Depends on:** T2
- **Reuses:** `cb_core.cache`, `cb_core.locales`
- **Done when:** every function is import-clean and side-effect free apart from
  `pending_posts`' cache calls.
- **Gate:** `uv run mypy packages/cb-core/src/cb_core/publisher.py packages/cb-core/src/cb_core/pending_posts.py`
- **Commit:** `feat(util_postforwarder): the caption pipeline and the pending-post cache`
- **→ R2, R5.2, R5.3, D-PF-4, D-PF-6, D-PF-10**

### T4 — Worker: `publisher_approve` (render + fan-out)

- **Skills:** /migrate-feature
- **What:** `prepare_post` and `schedule_post` as one arq job, per design R3–R5.
  Render: take the pending post, translate to pt and en through
  `llm.router()` (R5.1/R5.2), convert prices (R5.3), build the keyboard, send
  both captions to `postmail_chat_id` — `parse_mode="HTML"` on the photo send
  only (D-PF-5 preserved). Fan-out: the R4.1 query, v1's skip order (R4.2),
  the deterministic `max_posts` trim (R4.3), R4.4's schedule times, and R4.5's
  report. Telemetry per R7.4. Must not import `cb_gateway`.
- **Where:** `packages/cb-worker/src/cb_worker/jobs/publisher.py` (new),
  `packages/cb-worker/src/cb_worker/main.py` (registration)
- **Depends on:** T1, T2, T3
- **Reuses:** `cb_worker/jobs/youtube.py`'s job shape, `cb_core.members.roster`
- **Done when:** the job registers in `WorkerSettings.functions` and the module
  imports with no `cb_gateway` reference.
- **Gate:** `uv run mypy packages/cb-worker/src/cb_worker/jobs/publisher.py`
- **Commit:** `feat(util_postforwarder): render and fan out an approved post`
- **→ R3, R4, R5, D-PF-7, D-PF-12**

### T5 — Worker: `deliver_scheduled_posts` cron

- **Skills:** /migrate-feature
- **What:** Design R7. Five-minute cron; per due row decrement-or-delete first
  (D-PF-9 preserved), re-check `publisher_post` and delete when off (D-PG-4
  preserved), forward from `source_chat_id` with `message_thread_id` only when
  the chat `is_forum` and `thread_posts` is not NULL (D-PG-1 fixed). R7.3's
  failure taxonomy: kick / missing chat delete the row, everything else logs
  and retries next tick.
- **Where:** `packages/cb-worker/src/cb_worker/jobs/publisher.py`,
  `packages/cb-worker/src/cb_worker/main.py` (cron registration)
- **Depends on:** T1, T4
- **Reuses:** `main.py`'s `expire_captchas` cron shape
- **Done when:** `cron(deliver_scheduled_posts, minute={0,5,…,55})` is registered.
- **Gate:** `uv run mypy packages/cb-worker`
- **Commit:** folded into T4's commit
- **→ R7, D-PG-1, D-PF-8**

### T6 — Gateway: `/divulgar`, `/repost`, callbacks, reply relay

- **Skills:** /migrate-feature
- **What:** `handlers/publisher.py`. `/divulgar` with its three precondition
  branches and their exact strings; `build_approval_request` (exported for
  `util_postgetter`); the `SendToApprovalPub` callback → forward to the
  approval chat + the five-button `Approve post?` prompt; `yPub` → enqueue
  `PUBLISHER_APPROVE`, authorised by R6.1; `nPub` → evict the cache entry
  (v1's `deny_post`, including its early return on a one-field payload);
  `/repost` with its admin gate, reply requirement, numeric-days validation,
  daytime random window and 👍 reaction; the reply relay as its own router
  per R6.3. Registration in `build_router` at both required positions.
- **Where:** `packages/cb-gateway/src/cb_gateway/handlers/publisher.py` (new),
  `packages/cb-gateway/src/cb_gateway/handlers/__init__.py`
- **Depends on:** T1, T2, T3
- **Reuses:** `handlers/calladms.py`'s callback wire pattern,
  `cb_gateway.queue.enqueue`, `cb_gateway.context.context_for`/`t`
- **Done when:** every string in `spec.md`'s table is reachable from a branch.
- **Gate:** `uv run mypy packages/cb-gateway`
- **Commit:** `feat(util_postforwarder): the submit, approve and repost commands`
- **→ R3, R6**

### T7 [P] — Unit tests

- **Skills:** /migrate-feature
- **What:** `cb_core.publisher`: the keyboard's five-step order and its 5-row
  entity cap, the origin-link exclusion, the hidden-author list, emoji→digit,
  end-emoji stripping, `convert_prices_in_text` (largest amount, last currency,
  the `code_from == code_target` early return of D-PF-6, per-paragraph failure
  degradation), `finalise_caption`'s substitutions and its 1020 truncation.
  Callback wire: `build`/`parse` round trip and every malformed shape.
  `/repost`: days parsing, the 9999 default, the 10–17 hour window.
  Fan-out: v1's skip order, and R4.3's trim arithmetic.
- **Where:** `packages/cb-core/tests/test_publisher.py` (new),
  `packages/cb-gateway/tests/test_publisher_handlers.py` (new),
  `packages/cb-worker/tests/test_publisher_job.py` (new)
- **Depends on:** T4, T5, T6
- **Gate:** `uv run pytest packages/cb-core/tests/test_publisher.py packages/cb-gateway/tests/test_publisher_handlers.py packages/cb-worker/tests/test_publisher_job.py -q`
- **Commit:** folded into T6's commit
- **→ all**

### T8 [P] — Integration tests

- **Skills:** /migrate-feature
- **What:** Against real Citus, via `qa/integration/factories.py`: a scheduled
  row round-trips; `due_before` returns only due rows; `advance_or_expire`
  decrements and deletes at the boundary; the `max_posts` trim leaves exactly
  `max_posts`; `delete_by_requester` removes rows across more than one target
  group; a Citus topology assertion that the per-group statements are
  `Task Count: 1` (AGENTS.md §4.6).
- **Where:** `qa/integration/test_scheduled_posts.py` (new),
  `qa/integration/test_citus_topology.py` (one added case)
- **Depends on:** T1
- **Gate:** `uv run pytest qa/integration/test_scheduled_posts.py -q`
- **Commit:** folded into T6's commit
- **→ R1**

### T9 — Acceptance

- **Skills:** /migrate-feature
- **What:** `qa/features/util_postforwarder.feature` — the two QA scenarios
  ported with the approval press and a scheduler tick made explicit steps
  (spec.md conflict 1), plus authored scenarios for `/divulgar`'s three
  refusals, the approval press from a chat that is not the approval chat, and
  `/repost`. Mock Telegram and the LLM/exchange-rate calls only (AGENTS.md §6);
  run the worker job inline through the fake-queue pattern
  `qa/test_util_everyone.py` established.
- **Where:** `qa/features/util_postforwarder.feature` (new),
  `qa/test_util_postforwarder.py` (new)
- **Depends on:** T6, T7
- **Gate:** `uv run pytest qa/test_util_postforwarder.py -q`
- **Commit:** `test(util_postforwarder): the approval workflow v1 never had a scenario for`
- **→ all**

### T-final — Close out

- **Skills:** /migrate-feature, /review-changes, /lint-code
- **What:** `docs/contracts/util_postforwarder.md` with the Phase-2 and Phase-6
  tables; `scripts/spec.py` → `done`; `cb.py docs-sync`; the `.mdx` prose;
  `feature-map.mdx` rows for both QA conflicts and D-PF-12; `HANDOFF.md` §1
  and §4.
- **Where:** `docs/contracts/util_postforwarder.md`, `scripts/spec.py`,
  `docs/site/content/docs/feature-map.mdx`, `HANDOFF.md`
- **Depends on:** T1–T9
- **Gate:** `uv run python scripts/cb.py check`
- **Commit:** `docs(util_postforwarder): close out`
