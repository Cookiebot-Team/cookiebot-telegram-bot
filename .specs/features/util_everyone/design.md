# util_everyone — Design

Reads with `spec.md`. Requirement ids are back-referenced from `tasks.md`.

This slice builds one piece of shared infrastructure (R2, R3) before the feature
itself (R4, R5). Both are load-bearing for `util_calladms` and the captcha unban
afterwards, so they are designed as general mechanisms, not as `/everyone`
private helpers.

## R1 — The roster query

- **R1.1** Add to `packages/cb-core/src/cb_core/members.py` a single batched
  read, sibling to `random_usernames` (`members.py:139-148,201`):

  ```python
  async def roster(group_id: int) -> tuple[MemberRef, ...]
  ```

  where `MemberRef` is a frozen slotted dataclass carrying `user_id: int` and
  `username: str | None`. One statement, `WHERE group_members.group_id = $1 AND
  left_at IS NULL`, joined to the `users` reference table — a reference-table
  join is node-local, so this stays a single-shard read (AGENTS.md §4.4).
  Order deterministically (`ORDER BY user_id`) so the ping text is reproducible
  in tests.
- **R1.2** Both consumers use it: the handler takes the usernames for the ping
  text, the worker takes the `user_id`s for the DMs. Do **not** add a second
  query returning only usernames — one roster, two projections.
- **R1.3** `count(group_id)` (`members.py:219`) already exists and its docstring
  already names this feature; keep using it where a count is all that is needed.
- **R1.4** Export `MemberRef` and `roster` from `__all__` (`members.py:233`).

## R2 — Gateway → worker enqueue (new shared mechanism)

Nothing in the gateway can enqueue today: `cb-worker` registers only cron jobs
(`cb_worker/main.py:167-187`), and no `ArqRedis` pool is constructed anywhere in
`cb-gateway` (`calladms.py:24-32`, `groupguardian.py:504-507` both say so).

- **R2.1** New `packages/cb-gateway/src/cb_gateway/queue.py` owning one lazily
  created `arq` pool, built from the Redis/Valkey settings the gateway already
  uses for the group-config pub/sub and the cooldown store. **Do not add a
  second settings mechanism or a second Redis URL** (AGENTS.md §8) — read the
  existing one.
- **R2.2** Public surface, deliberately small:

  ```python
  async def enqueue(job: str, *args: object, **kwargs: object) -> bool
  async def close() -> None
  ```

  `enqueue` returns whether the job was accepted. It never raises into a
  handler: a broker failure is logged (`structlog`, event name `queue.enqueue`,
  `error=str(exc)`) and counted, and the reply the user already got stands.
- **R2.3** One counter, `cb_gateway_enqueue_total{job,outcome}`. `job` is the
  job name — a bounded set of literals — never a group or user id (AGENTS.md §7).
- **R2.4** `close()` is called from the gateway's existing shutdown path, next
  to whatever already closes the Redis client. If none exists, add it where the
  other clients are torn down.
- **R2.5** Job names are string constants shared between gateway and worker.
  Put them in `cb_core` (a `jobs.py` with `EVERYONE_FANOUT = "everyone_fanout"`)
  so a rename cannot silently desynchronise the two services.

## R3 — A worker that holds a bot (new shared mechanism)

HANDOFF §1 gap 1 and gap 5 both stall on this: the worker cannot talk to
Telegram.

- **R3.1** Construct one `aiogram.Bot` in the worker's `on_startup` and close it
  in `on_shutdown`, stored on the arq context (`ctx["bot"]`). Same token setting
  and same session configuration as the gateway — including the self-hosted API
  server base URL, which `cb-gateway` already honours (`platform_selfhosted_api`
  is `done`). A worker that ignores it would talk to the wrong endpoint.
- **R3.2** Do not import a gateway module from the worker. If bot construction
  lives in `cb_gateway`, move it to `cb_core` first and have both call it.
- **R3.3** This unblocks HANDOFF §1 gap 1 (captcha timeout cannot kick). Note
  that in the contract; do not implement it here.

## R4 — The reply path

- **R4.1** New `packages/cb-gateway/src/cb_gateway/handlers/everyone.py`
  exporting `router`, registered in the disjoint-commands block of
  `handlers/__init__.py`.
- **R4.2** Triggers: `CommandName("everyone")` for the slash forms —
  `cb_core/textmatch.py:56` already maps `everyone` — plus a mention matcher for
  bare `@everyone`, modelled on `calladms.py:152`
  (`_MENTION_TRIGGER = re.compile(r"^@adm(in)?\b", re.IGNORECASE)`). Verify the
  QA spelling `/ping everyone` also resolves; if it does not, add it to
  `COMMAND_ALIASES` **next to** `everyone`, never instead of it (AGENTS.md §2.1).
- **R4.3** Admin gate: `ctx.is_admin` from `context_for` — which already treats
  an anonymous `sender_chat` as an admin (`cb_core/admins.py`, and HANDOFF §3:
  "Telegram only allows `sender_chat` = the group for an admin"), matching v1's
  `sender_chat` bypass. Not an admin, **or the check could not be resolved** ⇒
  reply `t(ctx, "everyone_no")`, `mark_outcome`, return. That last clause is
  D-EV-2/D-EV-3: v2 fails closed where v1 failed open.
- **R4.4** Roster: `await members.roster(group_id)`. Usernames are the entries
  with a non-`None` `username`. Fewer than 2 ⇒ reply `t(ctx, "everyone_len")`
  and return (v1's `< 2`, `:107-110`).
- **R4.5** `sendChatAction typing`, then react `🫡` inside
  `contextlib.suppress(Exception)` (`ship.py:168-169`).
- **R4.6** Ping text, built by a **pure function** so it is unit testable
  without Telegram:

  ```python
  def ping_chunks(usernames: Sequence[str], known: int) -> list[str]
  ```

  First chunk starts with `f"Number of known users: {known}\n"` (D-EV-4:
  English, first chunk only). Each name is appended as `f"@{username} "`. A new
  chunk starts when `len(current) + len(username) + 2 > 4096`, reproducing
  `UserRegisters.py:113-120` exactly — same off-by-two, same boundary. `known`
  is `min(len(usernames), await bot.get_chat_member_count(chat_id))` (`:112`).
  Send each chunk with HTML parse mode.
- **R4.7** Then `enqueue(jobs.EVERYONE_FANOUT, group_id=…, chat_id=…,
  message_id=…, chat_title=…, lang=…)` — small scalars only. The worker
  re-reads the roster; do not ship a member list through the broker.
- **R4.8** Nothing else on the reply path. No `getChatMember` loop, no DM.

## R5 — The fan-out job

- **R5.1** New `packages/cb-worker/src/cb_worker/jobs/everyone.py` with
  `async def everyone_fanout(ctx, *, group_id, chat_id, message_id, chat_title,
  lang) -> None`, registered in `WorkerSettings.functions`
  (`cb_worker/main.py:167-187`) — the first non-cron job in the list.
- **R5.2** For each `MemberRef` from `members.roster(group_id)`:
  `get_chat_member`; status `left` or `kicked` ⇒ `await members.mark_left(
  group_id, user_id)` and skip (v1's registry cleanup, `:128-135`, now a
  status flip rather than a delete). Otherwise send the DM.
- **R5.3** DM body `locales.get("everyone_call", lang, title=chat_title)` with
  one inline button whose text is the v1 button label and whose URL is
  `https://t.me/c/{str(chat_id).removeprefix('-100')}/{message_id}` (`:139-146`).
  Read the button label verbatim from v1 rather than inventing one.
- **R5.4** Each send is individually suppressed — "bot blocked by user" is the
  common case and must not abort the fan-out (v1's bare `except`, `:139-146`).
  Keep v1's 0.1 s pause between sends, as `await asyncio.sleep(0.1)`.
- **R5.5** **No `forwardMessage`.** D-EV-5 is dropped; there is no owner id, no
  forward, no equivalent.
- **R5.6** Telemetry: `structlog` event `everyone.fanout` with `group_id` as a
  log field (logs may carry it; metrics may not), and a counter
  `cb_worker_everyone_dm_total{outcome}` with `outcome` in
  `sent|blocked|left|error`. No id labels.

## R6 — Tests

- **R6.1** Unit (`packages/cb-gateway/tests/test_everyone.py`): every trigger
  spelling incl. bare `@everyone` and the QA form; `ping_chunks` — header on the
  first chunk only, no chunk exceeding 4096, the boundary hit exactly at v1's
  condition, a single-member roster, and `known` clamped by the member count.
- **R6.2** Unit (`packages/cb-worker/tests/test_everyone_fanout.py`): the
  deep-link URL for a `-100…` supergroup id and for an id without the prefix;
  the `left`/`kicked` branch calling `mark_left`; a raising send not aborting
  the loop.
- **R6.3** Integration (`qa/integration/test_everyone.py`): seed a group and
  several members with `qa/integration/factories.py`, assert `roster` returns
  them ordered and excludes members marked left, and assert the query is
  single-shard the way `qa/integration/test_citus_topology.py` already asserts
  it for other hot queries.
- **R6.4** Acceptance (`qa/features/util_everyone.feature`,
  `qa/test_util_everyone.py`): the two QA scenarios wording-intact, plus a
  net-new scenario for the "fewer than two known members" path. The enqueue call
  is asserted against a fake queue — mock the outside world, not our handler
  (AGENTS.md §6).

## R7 — Docs

- **R7.1** `docs/contracts/util_everyone.md` — Phase-2 table, Phase-6 parity
  table, and the six D-EV verdicts. D-EV-5 (owner forward dropped) gets its own
  paragraph.
- **R7.2** `docs/contracts/` also gains the enqueue mechanism where it belongs:
  either a short section in this contract or a note in
  `docs/site/content/docs/architecture.mdx` §2 — one place, referenced from the
  other, not two descriptions.
- **R7.3** HANDOFF §1 gap 5 is now closed; gap 1 is unblocked but still open.
  Update both lines. HANDOFF §4 row 1 is done.
- **R7.4** `scripts/spec.py` status → `done`, then `cb.py docs-sync`.

## Open decisions — answered

1. **Fail open or closed when admin status is unknown?** Closed (D-EV-2/3). An
   unauthenticated everyone-ping is abuse; v1's behaviour there is a silent
   failure, and `/migrate-feature` Phase 2 says silent-failure bugs get fixed.
2. **Ship the roster through the broker?** No — scalars only (R4.7). The worker
   re-reads, so a queued job that runs late pings the roster as it is then, and
   the payload stays small.
3. **Port the owner forward?** No (D-EV-5).
4. **Delete stale members or mark them left?** Mark left (R5.2). v2's registry
   models departure with `left_at`; deleting the row would lose `first_seen_at`.
5. **Does this slice also move the captcha unban and `util_calladms`' DMs onto
   the new queue?** No. Build the mechanism, port one feature onto it, leave the
   other two as named follow-ups.
