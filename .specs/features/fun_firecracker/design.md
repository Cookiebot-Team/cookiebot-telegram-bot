# fun_firecracker — Design

Reads with `spec.md`. Requirement ids are back-referenced from `tasks.md`.

## R1 — Module placement

- **R1.1** New handler `packages/cb-gateway/src/cb_gateway/handlers/firecracker.py`,
  exporting `router`. Shape copied from `handlers/ship.py` — same fun-gate
  semantics (reply, not silence), same suppressed reaction call.
- **R1.2** Registered in `handlers/__init__.py` in the "commands: disjoint
  triggers, order irrelevant" block, next to `dice.router` and `ship.router`.
  It must **not** go in the join chain — it handles no join event.
- **R1.3** No `cb_core` change. `COMMAND_ALIASES` already maps `firecracker`,
  `rojao`, `rojão`, `acende`, `fogos` → `firecracker`
  (`cb_core/textmatch.py:47-48`), so `CommandName("firecracker")` is the filter.
- **R1.4** The burst runs inside the handler with `await` between sends. aiogram
  dispatches each update in its own task, so a long sequence does not block
  other updates; no worker job, no `asyncio.create_task`. AGENTS.md §2.4 targets
  slow *work* (ffmpeg, LLM, fan-out to other chats) — this is N sends to the
  chat that asked, which is the feature.

## R2 — Randomness and testability

- **R2.1** `random.randint` / `random.random` are called through a module-level
  `_rng: random.Random` so tests can seed it, matching the idiom already used by
  `dice.py` / `ship.py`. Do not invent a new seeding mechanism — reuse whatever
  those two handlers do; if they call `random` directly, add
  `rng: random.Random | None = None` as a keyword argument to the pure
  sequence-builder function instead of touching the module.
- **R2.2** The burst is computed by a **pure function** so the maths is unit
  testable without Telegram:
  `def burst(rng: random.Random) -> list[str]` returning the list of message
  bodies (the `"pra " * n` strings only — not the fuse or the bang). Invariants
  a unit test asserts: `sum(len(m) // 4 for m in burst) == amount` drawn,
  `5 <= amount <= 20`, every element is `"pra " * k` with `k >= 1`, and the list
  is non-empty. The handler then sends fuse → each burst line → bang.

## R3 — Gate behaviour

- **R3.1** `ctx = await context_for(bot, message)`; if the fun feature is
  disabled, reply with `t(ctx, "fun_off")` and return. Use whichever of
  `ctx.enabled("fun")` / `deny_if_disabled(...)` `ship.py` uses — one idiom, not
  two (AGENTS.md §8).
- **R3.2** `mark_outcome(...)` on the refused path, as `ship.py:159` and
  `dice.py:274` do. No new metric, and never a `group_id` label (AGENTS.md §7).

## R4 — Output fidelity

- **R4.1** The three strings are module-level constants with a comment naming
  `Miscellaneous.py:228/236/238`, and a comment recording D-FC-1 (deliberately
  unlocalised).
- **R4.2** The bang is sent with HTML parse mode; the fuse and the burst lines
  are plain. v1 sends all three through the same `send_message` whose default
  parse mode is HTML — `"fiiiiiiii.... "` and `"pra "` contain no markup, so
  HTML for all three is equivalent and is what to ship.
- **R4.3** Reaction `🎉`, `is_big=True`, wrapped in `contextlib.suppress(Exception)`
  (`ship.py:168-169`). A group where the bot cannot react still gets the sequence.
- **R4.4** `await asyncio.sleep(0.1)` after the fuse, before the burst
  (`Miscellaneous.py:229`). Nothing between the burst lines.

## R5 — Tests

- **R5.1** Unit — `packages/cb-gateway/tests/test_firecracker.py`: alias
  resolution for all five triggers (with argument, with `@botname`), and the
  `burst()` invariants of R2.2 over a seeded rng and over 1000 random seeds.
- **R5.2** Integration — none required. The feature touches no table. Do not add
  an empty integration test to satisfy the pyramid; say so in the contract.
- **R5.3** Acceptance — `qa/features/fun_firecracker.feature` (copied from
  `../Cookiebot-QA`, wording preserved) plus a new second scenario for the
  fun-off gate, driven by `qa/test_fun_firecracker.py`. Take update ids from
  `next_update_id()` (HANDOFF §5).

## R6 — Docs

- **R6.1** `docs/contracts/fun_firecracker.md` — Phase-2 table from `spec.md`
  plus a Phase-6 parity table (v1 behaviour vs v2 behaviour, one row each,
  "identical" or the drift).
- **R6.2** `scripts/spec.py` status `planned` → `done`, then `cb.py docs-sync`.

## Open decisions — answered

1. **Localise the three strings?** No (D-FC-1, preserve).
2. **Throttle the burst?** No added sleeps (D-FC-2). If Telegram flood-limits a
   send, aiogram raises; the sequence stops there, which is v1's behaviour too.
3. **Worker job?** No (R1.4).
