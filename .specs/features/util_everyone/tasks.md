# util_everyone — Tasks

Grammar: `tlc-spec-driven` §5. Read `spec.md` and `design.md` first.

T1–T3 are shared infrastructure with their own value: they close HANDOFF §1
gap 5 and unblock gap 1. T4–T6 are the feature.

## Status

| Task | Status | Notes |
|------|--------|-------|
| T1 [P] — Batched roster query | ✅ done | one query, single shard |
| T2 [P] — Gateway → worker enqueue | ✅ done | closes HANDOFF §1 gap 5 |
| T3 — Worker holds a bot | ✅ done | unblocks HANDOFF §1 gap 1 |
| T4 — Reply path: gate, roster, chunked ping | ✅ done | depends on T1, T2 |
| T5 — DM fan-out job | ✅ done | depends on T1, T3 |
| T6 — Acceptance scenarios | ✅ done | depends on T4, T5 |
| T-final — Close out | ✅ done | contract, HANDOFF gaps, spec.py flip |

## Tasks

### T1 [P] — Batched roster query

- **Skills:** /implement-feature
- **What:** Add `MemberRef` (frozen, slotted: `user_id: int`,
  `username: str | None`) and `async def roster(group_id: int) ->
  tuple[MemberRef, ...]` to the member registry, per design R1. One statement,
  `WHERE group_members.group_id = $1 AND left_at IS NULL`, joined to the `users`
  reference table, `ORDER BY user_id`. Export both from `__all__`. This replaces
  v1's N+1 (`UserRegisters.py:129`, one backend call per member) with a single
  single-shard read — that is the whole point of the port, so the integration
  test asserting `Task Count: 1` is part of this task, not a later one.
- **Where:** `packages/cb-core/src/cb_core/members.py`,
  `packages/cb-core/tests/test_members.py`,
  `qa/integration/test_everyone.py` (new).
- **Depends on:** none
- **Reuses:** `members.py:139-148` (`_RANDOM_USERNAMES` — same shape, without
  `ORDER BY random() LIMIT`), `members.py:201` (`random_usernames`) for the
  connection idiom, `qa/integration/factories.py` for seeding,
  `qa/integration/test_citus_topology.py` for the `EXPLAIN` assertion style.
- **Done when:** `roster` returns seeded members in `user_id` order, excludes
  members marked left, and the topology test shows a single-shard plan.
- **Gate:** `uv run pytest packages/cb-core/tests/test_members.py qa/integration/test_everyone.py -q`
- **Commit:** `feat(cb-core): read a group's whole roster in one query`
- **→ R1, R6.3**

### T2 [P] — Gateway → worker enqueue

- **Skills:** /implement-feature
- **What:** Build the mechanism the project has been deferring — see
  `calladms.py:24-32` and `groupguardian.py:504-507`, which both say it does not
  exist. New `cb_gateway/queue.py` owning one lazily created `arq` pool built
  from the Redis/Valkey settings the gateway **already** uses for group-config
  pub/sub and cooldowns (do not add a second URL or a second settings
  mechanism). Surface: `async def enqueue(job, *args, **kwargs) -> bool` and
  `async def close() -> None`. `enqueue` never raises into a handler: log
  `queue.enqueue` with `error=str(exc)`, count it, return `False`. One counter
  `cb_gateway_enqueue_total{job,outcome}` — `job` only, never an id. Job-name
  constants go in a new `cb_core/jobs.py` so gateway and worker cannot drift.
  Wire `close()` into the gateway's existing shutdown path.
- **Where:** `packages/cb-gateway/src/cb_gateway/queue.py` (new),
  `packages/cb-core/src/cb_core/jobs.py` (new), the gateway's app
  startup/shutdown module, `packages/cb-gateway/tests/test_queue.py` (new).
- **Depends on:** none
- **Reuses:** the existing Redis/Valkey settings object and client construction;
  `cb_gateway/telemetry.py` for the counter.
- **Done when:** a unit test shows `enqueue` returning `True` against a fake
  pool, returning `False` and logging when the pool raises, and never
  propagating an exception.
- **Gate:** `uv run pytest packages/cb-gateway/tests/test_queue.py -q && uv run python scripts/cb.py types`
- **Commit:** `feat(cb-gateway): a way to hand work to the worker`
- **→ R2**

### T3 — Worker holds a bot

- **Skills:** /implement-feature
- **What:** Give `cb-worker` an `aiogram.Bot` built in `on_startup` and closed in
  `on_shutdown`, on `ctx["bot"]`, per design R3. Same token setting and the same
  session configuration as the gateway, **including the self-hosted API server
  base URL** — the worker must not silently talk to api.telegram.org while the
  gateway talks to the self-hosted server. If bot construction currently lives
  in `cb_gateway`, move it to `cb_core` and have both services call it; the
  worker must not import a gateway module.
- **Where:** `packages/cb-worker/src/cb_worker/main.py`, possibly a new
  `packages/cb-core/src/cb_core/bot.py`, `packages/cb-gateway/src/…` (call
  site if the constructor moved), `packages/cb-worker/tests/test_startup.py`.
- **Depends on:** none (independent of T1/T2, but sequenced after them because
  it touches worker startup that T5 then extends)
- **Reuses:** the gateway's existing `Bot` construction, verbatim.
- **Done when:** the worker starts with a bot on its context, closes it on
  shutdown, and a unit test asserts the base URL follows the self-hosted setting.
- **Gate:** `uv run pytest packages/cb-worker/tests -q && uv run python scripts/cb.py types`
- **Commit:** `feat(cb-worker): a bot the jobs can talk to Telegram with`
- **→ R3**

### T4 — Reply path: gate, roster, chunked ping

- **Skills:** /migrate-feature (Phases 4–5)
- **What:** New handler per design R4. Triggers: `CommandName("everyone")`,
  a `^@everyone\b` mention matcher, and the QA spelling `/ping everyone` —
  verify all three resolve, adding any missing alias **next to** the existing
  one. Admin gate on `ctx.is_admin`, which already passes anonymous
  `sender_chat` admins; not an admin **or unresolvable** ⇒ `everyone_no`,
  `mark_outcome`, return (D-EV-2/D-EV-3 — v2 fails closed where v1 failed open;
  this is a deliberate divergence and must be commented as one). Roster from
  T1; fewer than two usernames ⇒ `everyone_len`. Then `typing` action, `🫡`
  reaction (suppressed), and the chunks from a pure
  `ping_chunks(usernames, known) -> list[str]`: header
  `f"Number of known users: {known}\n"` on the **first chunk only**, English,
  unlocalised (D-EV-4); a new chunk when
  `len(current) + len(username) + 2 > 4096`, reproducing
  `UserRegisters.py:113-120` including its off-by-two; `known` is
  `min(len(usernames), get_chat_member_count(chat_id))`. Finish by enqueuing
  `jobs.EVERYONE_FANOUT` with scalars only. Write the unit tests first (design
  R6.1), red before the handler exists.
- **Where:** `packages/cb-gateway/src/cb_gateway/handlers/everyone.py` (new),
  `packages/cb-gateway/src/cb_gateway/handlers/__init__.py` (one import + one
  `include_router`, disjoint-commands block),
  `packages/cb-core/src/cb_core/textmatch.py` (only if an alias is missing),
  `packages/cb-gateway/tests/test_everyone.py` (new).
- **Depends on:** T1, T2
- **Reuses:** `handlers/calladms.py:152` (mention-trigger regex),
  `handlers/ship.py` (suppressed reaction, `mark_outcome`),
  `cb_gateway/context.py` (`context_for`, `t`), `cb_core/locale_data` for
  `everyone_no` / `everyone_len` — never retype a string.
- **Done when:** unit tests green, the router is registered once, and no DM or
  `get_chat_member` call happens on the reply path.
- **Gate:** `uv run pytest packages/cb-gateway/tests/test_everyone.py -q && uv run python scripts/cb.py types`
- **Commit:** `feat(util_everyone): ping the whole roster from one query`
- **→ R4, R6.1**

### T5 — DM fan-out job

- **Skills:** /implement-feature
- **What:** New arq job per design R5, registered in `WorkerSettings.functions`
  as the first non-cron entry. Per member from `members.roster(group_id)`:
  `get_chat_member`; `left`/`kicked` ⇒ `members.mark_left(group_id, user_id)`
  and skip (v1 deleted the register row, `UserRegisters.py:128-135`; v2 marks
  left so `first_seen_at` survives). Otherwise DM
  `locales.get("everyone_call", lang, title=chat_title)` with one inline button
  — label read verbatim from v1, URL
  `https://t.me/c/{str(chat_id).removeprefix('-100')}/{message_id}`. Each send
  individually suppressed (blocked-by-user is the common case), `await
  asyncio.sleep(0.1)` between sends. **Do not port the every-10th
  `forwardMessage` to the bot owner** (`:137-138`, D-EV-5) — it exfiltrates group
  content to a hardcoded account. Counter
  `cb_worker_everyone_dm_total{outcome}` with `outcome` in
  `sent|blocked|left|error`; no id labels.
- **Where:** `packages/cb-worker/src/cb_worker/jobs/everyone.py` (new),
  `packages/cb-worker/src/cb_worker/main.py` (registration),
  `packages/cb-worker/tests/test_everyone_fanout.py` (new).
- **Depends on:** T1, T3
- **Reuses:** an existing cron job in `cb_worker/` for the job signature and
  telemetry shape; `cb_core.members`, `cb_core.locales`, `cb_core.jobs`.
- **Done when:** unit tests cover the deep link for a `-100…` id and a bare id,
  the `left`/`kicked` branch, and a raising send not aborting the loop.
- **Gate:** `uv run pytest packages/cb-worker/tests/test_everyone_fanout.py -q && uv run python scripts/cb.py types`
- **Commit:** `feat(util_everyone): DM every member from the worker, not the reply path`
- **→ R5, R6.2**

### T6 — Acceptance scenarios

- **Skills:** /migrate-feature (Phase 3 + top of Phase 4)
- **What:** Copy `../Cookiebot-QA/features/util_everyone.feature` into
  `qa/features/` **wording unchanged**, then append a net-new scenario for the
  "fewer than two known members" path (`everyone_len`). Drive them against mock
  Telegram: admin ⇒ the ping chunks go out and one `EVERYONE_FANOUT` job is
  enqueued with the expected scalars, asserted against a fake queue; non-admin ⇒
  exactly the `everyone_no` text and no enqueue. Mock only the outside world.
- **Where:** `qa/features/util_everyone.feature` (new),
  `qa/test_util_everyone.py` (new).
- **Depends on:** T4, T5
- **Reuses:** `qa/conftest.py`, `qa/test_fun_ship.py` for pytest-bdd wiring,
  `next_update_id()` for every update.
- **Done when:** all three scenarios pass and no existing acceptance test
  regressed.
- **Gate:** `uv run pytest qa/test_util_everyone.py -q`
- **Commit:** `test(util_everyone): the QA scenarios and the empty-roster path`
- **→ R6.4**

### T-final — Close out

- **Skills:** none
- **What:** Write `docs/contracts/util_everyone.md` — Phase-2 table with v1
  `file:line` intact, Phase-6 parity table, and the six D-EV verdicts, with
  D-EV-5 (owner forward dropped) and D-EV-2/3 (gate now fails closed) each
  getting their own paragraph, since both are deliberate behavioural
  divergences. Document the enqueue mechanism in exactly one place and reference
  it from the other (design R7.2). Update `HANDOFF.md`: §1 gap 5 closed, gap 1
  unblocked but open, §4 row 1 done. Confirm the `/ping everyone` trigger
  mismatch is recorded in `docs/site/content/docs/feature-map.mdx`. Flip
  `util_everyone` to `done` in `scripts/spec.py`, run `cb.py docs-sync`, mark
  this file's Status rows done.
- **Where:** `docs/contracts/util_everyone.md` (new), `HANDOFF.md`,
  `docs/site/content/docs/feature-map.mdx`, `scripts/spec.py`, regenerated
  `docs/site/**`, `.specs/features/util_everyone/tasks.md`.
- **Depends on:** T6
- **Reuses:** `docs/contracts/util_calladms.md` and `docs/contracts/fun_ship.md`
  as the format.
- **Done when:** `cb.py check` exits 0.
- **Gate:** `uv run python scripts/cb.py check`
- **Commit:** `docs(util_everyone): close out`
- **→ R7**
