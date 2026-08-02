# Contract: fun_firecracker (v1 -> v2)

Phase 2/6 of `/migrate-feature` for `/rojao`, `/rojão`, `/acende`, `/fogos` and
`/firecracker`. QA: `../Cookiebot-QA/features/fun_firecracker.feature`.
FEATURE-MAP row: `fun_firecracker`. Files owned by this port:
`packages/cb-gateway/src/cb_gateway/handlers/firecracker.py`,
`packages/cb-gateway/src/cb_gateway/handlers/__init__.py` (one router line),
`packages/cb-gateway/tests/test_firecracker.py`,
`qa/features/fun_firecracker.feature`, `qa/test_fun_firecracker.py`, this file.

## Phase 2 — v1 behaviour contract

v1 handler: `firecracker`, `../COOKIEBOT-Telegram-Group-Bot/Bot/Miscellaneous.py:226-238`.
Dispatch: `../COOKIEBOT-Telegram-Group-Bot/Bot/COOKIEBOT.py:215,230-231`.

| Aspect | v1 behaviour (file:line) |
|---|---|
| Triggers | `/rojao`, `/rojão`, `/acende`, `/fogos`, `/firecracker` — `startswith` prefix match, so trailing text and `@botname` still fire (`COOKIEBOT.py:230-231`; alias tuple also at `:215`) |
| Preconditions | group/supergroup only — channels return early (`COOKIEBOT.py:73-74`), private chats never reach the fun chain (`:75-110`); message must have `text` starting with `/` (`:185-186`). Gated on `functionsFun`: when off the user is **told**, not ignored — `notify_fun_off` replies with locale key `fun_off` (`COOKIEBOT.py:218-219`, `Miscellaneous.py:129-131`). No admin check. |
| Cooldowns / quotas | none — `Bot/Cooldowns.py` has no entry for this command |
| Success output | fixed sequence (`Miscellaneous.py:226-238`): ① react `🎉` ② reply `"fiiiiiiii.... "` to the trigger ③ sleep 0.1s ④ loop: `amount = randint(5, 20)`; while `amount > 0`: coin flip picks `n = randint(1, amount)` or `n = 1`, send `"pra " * n`, `amount -= n` ⑤ send `"<b> 💥POOOOOOOWW💥 </b>"` (HTML). Only ② is a reply; ④ and ⑤ are plain sends. Message count is variable, not fixed. |
| Failure output | none. No try/except in the handler; an exception is swallowed by the dispatcher's bare `except`, so the user sees a partial sequence and no error. |
| Persistence | none |
| Side effects | one `setMessageReaction` call (`universal_funcs.py:300-305`) and between 2 and ~21 `sendMessage` calls per invocation, unthrottled after the initial 0.1s sleep |
| External calls | Telegram Bot API only |
| Known defects | D-FC-1, D-FC-2 below |

### Verbatim strings

Hardcoded in `Miscellaneous.py`, **not** in any locale file:

| String | Source |
|---|---|
| `fiiiiiiii.... ` | `Miscellaneous.py:228` |
| `pra ` (repeated `n` times per message) | `Miscellaneous.py:236` |
| `<b> 💥POOOOOOOWW💥 </b>` | `Miscellaneous.py:238` |

The only locale-backed string reachable through this feature is the gate notice,
key `fun_off` in `Bot/Static/locales/{eng,pt,es}/lib.json` (eng:119, pt:131,
es:114) — already ported to `cb_core/locale_data/`.

### Defects — verdict per item

| id | Defect | Verdict |
|---|---|---|
| D-FC-1 | Never localised: `firecracker()` takes no `language` argument, so `send_message` falls through on its `language="pt"` default and `translate()` never fires (`universal_funcs.py:195-198`). Output is byte-identical in every language. | **preserved.** The three strings are onomatopoeia, not words; localising them would change observable output for every existing group. `firecracker.py`'s `_FUSE`, `_PRA` and `_BANG` constants are sent as-is, with no `t(ctx, ...)` call anywhere in the success path — only the gate-off reply goes through `t()`. |
| D-FC-2 | Flood risk: up to ~21 sends with no throttle between them. | **preserved, bounded.** Same message count and the same 0.1s pre-loop pause (`await asyncio.sleep(0.1)`), no per-message sleeps added. The risk is bounded by design R1.4 instead: aiogram dispatches each update in its own task, so the burst never blocks other chats' updates — no worker job, no queue. If Telegram flood-limits a send mid-burst, aiogram raises and the sequence stops there, which is v1's behaviour too (an unhandled exception mid-loop). |

## Phase 6 — parity table

| Aspect | Verdict |
|---|---|
| Triggers (`/rojao`, `/rojão`, `/acende`, `/fogos`, `/firecracker`, bare/with argument/with `@botname`) | **identical** — resolved through `cb_core/textmatch.py:47-48`'s existing `COMMAND_ALIASES` entry, no new alias work; asserted by a parametrised unit test |
| Gate (`functionsFun`) and its reply | **identical** — `ctx.enabled("fun")`, one `fun_off` reply, nothing else sent; `mark_outcome("refused")` recorded (no new metric, no `group_id` label) |
| Reaction | **identical in emoji and `is_big`, changed in failure handling** — v1's `react_to_message` has no error handling of its own but is swallowed by the dispatcher's bare `except`; v2 wraps the same call in `contextlib.suppress(Exception)` explicitly, so a group where the bot cannot react still gets the rest of the sequence either way |
| Fuse reply (`"fiiiiiiii.... "`) | **identical** — byte-identical string, sent as a reply to the trigger message |
| Pre-burst pause | **identical duration (0.1s), changed mechanism** — `await asyncio.sleep(0.1)` instead of `time.sleep(0.1)`, so the event loop is not blocked; observable delay to the user is the same |
| Burst maths (`amount = randint(5, 20)`, coin-flip `n`, `"pra " * n` per line) | **identical** — ported byte-for-byte into the pure `burst()` function; invariants (`5 <= amount <= 20`, total `pra` count equals the drawn amount, every line `"pra " * k` with `k >= 1`, non-empty) checked over a seeded `random.Random` and over 1000 seeds in `test_firecracker.py` |
| Bang (`"<b> 💥POOOOOOOWW💥 </b>"`) | **identical** — byte-identical string, HTML rendered because the `Bot` instance's default `parse_mode` is HTML, same as v1's single shared `send_message` call — no explicit `parse_mode=` needed on any of the three sends |
| Reply vs send | **identical** — only the fuse is a reply (`message.reply`); the burst lines and the bang are plain sends (`message.answer`) |
| Localisation of the three literals (D-FC-1) | **identical** — preserved unlocalised, see above |
| Flood risk (D-FC-2) | **identical message count and timing, changed non-blocking guarantee** — see above |
| Failure mid-sequence | **identical** — no try/except around the burst/bang; an aiogram exception (e.g. a flood-control error) stops the sequence partway with no user-visible error, matching v1's swallowed-exception behaviour |
| Random source | **identical distribution** — Python's `random` module called directly (`random.randint`, `random.random`), matching `ship.py`'s and `dice.py`'s existing idiom rather than threading a `random.Random` instance through the module (design R2.1); the production call site takes no seed |

## Design R5.2 — no integration test

The feature touches no table: `firecracker.py` reads nothing but the group's
`fun` gate through `context_for` (already covered by every other gated
handler's integration tests) and persists nothing of its own. There is no
Citus distribution, no migration, no write path unique to this feature to
integration-test. Adding an empty `qa/integration/test_firecracker.py` would
only assert that the mock DB doesn't blow up on a lookup already exercised
elsewhere — it would not exercise anything specific to this feature, so it was
not written. Coverage instead comes from the unit tests (pure `burst()` maths,
alias resolution) and the acceptance tests (the full sequence, and the gate-off
reply, against the mock Telegram API).

## Tests

| Layer | File |
|---|---|
| Unit — trigger/alias resolution, `burst()` invariants over a seeded rng and 1000 seeds | `packages/cb-gateway/tests/test_firecracker.py` |
| Integration — none; see R5.2 above | n/a |
| Acceptance — the Gherkin scenarios, including the net-new fun-off gate scenario | `qa/features/fun_firecracker.feature`, `qa/test_fun_firecracker.py` |
