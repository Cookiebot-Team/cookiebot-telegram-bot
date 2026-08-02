# util_calladms — Tasks (DM half)

## Status

| Task | Status | Notes |
|------|--------|-------|
| T1 — Job-name constant | ✅ done | |
| T2 — Worker job `notify_admins_of_call` | ✅ done | |
| T3 — Register the job in `cb-worker` | ✅ done | |
| T4 — Wire the gateway enqueue call | ✅ done | |
| T5 [P] — Unit tests for the job | ✅ done | |
| T6 — Acceptance: rewrite the DM step | ✅ done | |
| T-final — Close out | ✅ done | |

## T1 — Job-name constant

- **Skills:** /migrate-feature
- **What:** Add `CALLADMS_NOTIFY_ADMINS = "notify_admins_of_call"` to
  `cb_core/jobs.py`, next to `EVERYONE_FANOUT`, with a one-line doc comment
  pointing at the worker module and the gateway call site (same shape as the
  existing `EVERYONE_FANOUT` comment).
- **Where:** `packages/cb-core/src/cb_core/jobs.py`
- **Depends on:** none
- **Done when:** the constant exists and is exported in `__all__`.
- **Gate:** `uv run ruff check packages/cb-core/src/cb_core/jobs.py`
- **Commit:** n/a (folded into T2)
- **→ R2.2 (design.md)**

## T2 — Worker job `notify_admins_of_call`

- **Skills:** /migrate-feature
- **What:** New module implementing `_notify` and `notify_admins_of_call` per
  design R3.1-R3.3: sorted fresh `cb_core.admins.admin_ids`, `bot.id`
  exclusion, `notification_admin` locale string, conditional deep-link button
  (`'-100' in str(group_id)`, v1's exact substring test), per-send
  suppression, `0.1s` throttle, `cb_worker_calladms_dm_total{outcome}`
  counter, job wrapper copied from `everyone.py`'s shape.
- **Where:** `packages/cb-worker/src/cb_worker/jobs/calladms.py` (new)
- **Depends on:** T1
- **Reuses:** `cb_core.admins.admin_ids`, `cb_core.locales.get`,
  `cb_core.telemetry.span`/`context_from_carrier`, `cb_core.metrics.job_duration`
  — same imports `cb_worker/jobs/everyone.py` already uses.
- **Done when:** module exports `notify_admins_of_call`; a raising
  `admin_ids` call degrades to zero DMs (never raises, matching
  `cb_core.admins`'s own contract) rather than crashing the job.
- **Gate:** `uv run mypy packages/cb-worker/src/cb_worker/jobs/calladms.py`
- **Commit:** `feat(util_calladms): DM every admin from a cb-worker job`
- **→ R3 (design.md)**

## T3 — Register the job in `cb-worker`

- **Skills:** /migrate-feature
- **What:** Import `notify_admins_of_call` in `cb_worker/main.py` and add it
  to `WorkerSettings.functions` (arq only dispatches functions listed there —
  `everyone_fanout`'s own registration is the template). No cron entry: this
  job is enqueue-only, never scheduled.
- **Where:** `packages/cb-worker/src/cb_worker/main.py`
- **Depends on:** T2
- **Done when:** `WorkerSettings.functions` contains both fan-out jobs.
- **Gate:** `uv run mypy packages/cb-worker/src/cb_worker/main.py`
- **Commit:** folded into T2's commit
- **→ R1.2 (design.md)**

## T4 — Wire the gateway enqueue call

- **Skills:** /migrate-feature
- **What:** In `confirm_call_admins`, replace the
  `log.info("calladms.dm_fanout_not_implemented", ...)` line with
  `await enqueue(jobs.CALLADMS_NOTIFY_ADMINS, group_id=chat_id, chat_title=callback.message.chat.title or "", original_message_id=original_message_id, lang=ctx.lang)`.
  Update the module docstring: remove the "DM fan-out is not implemented
  here" numbered item and the file-ownership disclaimer it required; describe
  what the job does instead, same shape `everyone.py`'s docstring uses for
  its own fan-out paragraph.
- **Where:** `packages/cb-gateway/src/cb_gateway/handlers/calladms.py`
- **Depends on:** T1
- **Done when:** the handler imports `cb_core.jobs` and
  `cb_gateway.queue.enqueue`; the old log line and its docstring paragraph
  are gone.
- **Gate:** `uv run mypy packages/cb-gateway/src/cb_gateway/handlers/calladms.py && uv run pytest packages/cb-gateway/tests/test_calladms.py`
- **Commit:** `feat(util_calladms): enqueue the DM fan-out on confirm`
- **→ R1.1 (design.md)**

## T5 [P] — Unit tests for the job

- **Skills:** /migrate-feature
- **What:** `packages/cb-worker/tests/test_calladms_notify.py`, mirroring
  `test_everyone_fanout.py`'s structure: `_deep_link`'s `-100`-substring
  behaviour (present and absent), the bot excluded from its own DM loop, a
  raising `send_message` for one admin not aborting the rest, an empty
  `admin_ids()` result sending nothing (Telegram-outage degrade path), and
  the `notify_admins_of_call` wrapper sourcing its bot from `ctx["bot"]`.
- **Where:** `packages/cb-worker/tests/test_calladms_notify.py` (new)
- **Depends on:** T2
- **Reuses:** `AsyncMock`-based fake bot pattern from `test_everyone_fanout.py`
- **Done when:** every case above has an assertion; `everyone_dm_total`-style
  counter deltas are asserted the same way `test_everyone_fanout.py` does.
- **Gate:** `uv run pytest packages/cb-worker/tests/test_calladms_notify.py -q`
- **Commit:** folded into T2's commit
- **→ R3, R4 (design.md)**

## T6 — Acceptance: rewrite the DM step

- **Skills:** /migrate-feature
- **What:** Add an autouse `fake_queue` fixture to
  `qa/test_util_calladms.py` (same shape as `qa/test_util_everyone.py`'s,
  patching `calladms_handler.enqueue`). Rewrite the `dm_confirmation` step
  (bound to the existing Gherkin line "should send a message on the adm's DM
  confirming that they have been pinged in a group" — wording unchanged) to
  assert one `CALLADMS_NOTIFY_ADMINS` job was enqueued with
  `group_id/chat_title/original_message_id/lang` matching the scenario's
  group and prompt, instead of asserting no DM call happened. Every other
  `then` step in this file keeps asserting `not fake_queue`/no DM as
  appropriate (decline, stale-button branches must still enqueue nothing).
- **Where:** `qa/test_util_calladms.py`
- **Depends on:** T4
- **Done when:** `qa/features/util_calladms.feature` runs green unmodified —
  no wording changes to the `.feature` file itself.
- **Gate:** `uv run pytest qa/test_util_calladms.py -q`
- **Commit:** `test(util_calladms): assert the DM fan-out is enqueued on confirm`
- **→ QA section (spec.md)**

## T-final — Close out

- **Skills:** none
- **What:** Update `docs/contracts/util_calladms.md` (close the "Decision:
  this needs a cb-worker job" section with what actually shipped, add a
  Phase 6 parity table). Flip `util_calladms`'s `status` to `Status.DONE` and
  `layer` to `Layer.WORKER` in `scripts/spec.py` (matches `util_everyone`'s
  precedent — the feature's defining characteristic is now the fan-out).
  Run `cb.py docs-sync`, write real prose into
  `docs/site/content/docs/features/util_calladms.mdx`'s two empty sections.
  Update the `util_calladms` row in
  `docs/site/content/docs/feature-map.mdx` if its note needs to change.
  Update `HANDOFF.md` §1 gap 5 and §4's table.
- **Where:** as listed above
- **Depends on:** T1-T6
- **Done when:** `uv run python scripts/cb.py check` exits 0.
- **Gate:** `uv run python scripts/cb.py check`
- **Commit:** `docs(util_calladms): close out`
