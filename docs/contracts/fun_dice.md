# Contract: fun_dice (v1 -> v2)

Phase 2/6 of `/migrate-feature` for `/dado`, `/dice`, `/d<N>` and QA's `roll`
spelling. QA: `../Cookiebot-QA/features/fun_dice.feature`. FEATURE-MAP row:
`fun_dice`, status `❌ spec/code trigger mismatch`. Files owned by this port:
`packages/cb-gateway/src/cb_gateway/handlers/dice.py`, `qa/features/fun_dice.feature`,
`qa/test_fun_dice.py`, `packages/cb-gateway/tests/test_dice.py`, this file.

## Two corrections to the task brief, found while reading v1

1. **The gate is `functionsUtility`, not `functionsFun`.** Dice sits in the
   second `elif` chain of the dispatcher (`COOKIEBOT.py:248-255`, which checks
   `utilityfunctions`), not the first (`:214-217`, which checks `funfunctions`
   and does not even list `/dado`/`/dice` among its triggers). `docs/site/content/docs/feature-map.mdx`
   files `fun_dice` under its "Fun" section header purely because that is the
   name of the QA spec file — the runtime gate is unrelated to that grouping.
   `cb_core/group_config.py`'s own `_FEATURE_AREAS` docstring and
   `cb_gateway/filters.py`'s `FeatureGate` docstring already cite both line
   numbers correctly (218 = fun, 252 = utility); this port is the first
   fun-categorised feature to actually need the distinction right.
2. **`FeatureGate` would be the wrong tool even for the correct area.** Its own
   docstring says a gated-off command in v1 "simply is not dispatched — no
   error, no reply". That is false for this command family: v1's
   `notify_utility_off` (`Miscellaneous.py:133-135`) explicitly replies with the
   `utility_off` catalog string (`send_message`'s 4th positional argument is
   `msg_to_reply`). `fun_random.py` (a sibling port, `functions_fun`/`fun_off`)
   already reached the identical conclusion for its own gate and checks
   `ctx.enabled("fun")` inline rather than filtering; this port does the same
   for `"utility"`/`utility_off`.

## Phase 2 — v1 behaviour contract

v1 handler: `roll_dice`, `../COOKIEBOT-Telegram-Group-Bot/Bot/Miscellaneous.py:160-183`.
Dispatch: `../COOKIEBOT-Telegram-Group-Bot/Bot/COOKIEBOT.py:248-255`.

| Aspect | v1 behaviour (file:line) |
|---|---|
| Triggers | `/dado`, `/dice` (`COMMAND_ALIASES` already has both, plus `roll`), and a `/d<N>` shorthand recognised by the dispatcher's own condition: `msg['text'].startswith("/d") and msg['text'].split()[0].split('/d')[1].isdigit()` (`COOKIEBOT.py:249`) — no upper bound on digit count. `@botname` forms and trailing arguments are all still prefix-matched. `roll` (QA's spelling) has **no v1 trigger at all** — see the "spec/code trigger mismatch" note below. |
| Preconditions | Gated by `functionsUtility`, not `functionsFun` (see correction #1 above). No admin check. |
| Cooldowns / quotas | None. |
| Success output — `/dado`, `/dice` | **Always** the `dice_exemple` usage text, regardless of any trailing argument: `start = msg['text'].split(" ")[0]`; `if start in ("/dado", "/dice"): ...` compares only the first whitespace token, so `/dado 6` still shows the example and never rolls. Sent with `send_message(cookiebot, chat_id, text)` — **no** `msg_to_reply`, i.e. a plain send, not a reply. |
| Success output — `/d<N>` | Rolls `N`-sided (`limite`) `vezes` times. `vezes` defaults to `1` when the command has no second token; otherwise `vezes = max(min(20, int(second_token)), 1)` (`Miscellaneous.py:171`) — clamped to `[1, 20]`, never an error for an out-of-range count. A single roll: `f"(d{limite}) 🎲 -> {roll}"`. Several rolls: `f"(d{limite}) "` followed by one `dice_roll`-catalog line per roll (`"\n%(vez)sth Roll: 🎲 -> %(roll)s"` in English — note the literal, ungrammatical "th" on every ordinal). Sent with `msg_to_reply=msg` — a reply. |
| Failure output | **None in v1's source, but two real crash paths**: a non-numeric second token, or `limite` parsing to `0` (`"0".isdigit()` is `True`, so `/d0` dispatches) both reach a bare, uncaught `int(...)`/`random.randint(1, 0)` that raises inside `roll_dice`. Every v1 message handler runs inside a bare `except Exception:` (`COOKIEBOT.py:329,432`) that only prints and moves on — **the user sees nothing at all**. Fixed in this port, not preserved (see Phase 6). |
| Persistence | None. |
| Side effects | `send_chat_action(cookiebot, chat_id, 'typing')` before either branch — cosmetic, dropped by every other ported handler in this codebase (`core_privacy.md`, `core_rules.md`) and dropped here too, for the same reason. |
| External calls | None. |
| Known defects | The silent-crash paths above are this feature's own; none of FEATURE-MAP's D1-D13 apply directly. |

### The QA-spec / v1 trigger mismatch (FEATURE-MAP's own flag)

QA's `fun_dice.feature` speaks only `roll 6` / `roll 20` / `roll` — a bare word,
no leading slash, and a spelling v1 never implements at all. v1 ships `/dado`,
`/dice` and `/d<N>`, none of which behave the way QA's three scenarios expect
(`/dado`/`/dice` never take a "sides" argument; only `/d<N>` rolls, and `N` is
part of the command name, not an argument). Per AGENTS.md §2.1 ("no new
command name without an alias... never instead of the old one") and the
migrate-feature skill ("where they disagree, record both, implement both"),
this port:

- Keeps every v1 form working exactly as before (`/dado`, `/dice`, `/d<N>`).
- Adds `roll <N> [times]` as a **new** canonical trigger (already present in
  `cb_core/textmatch.py:COMMAND_ALIASES` as `"roll": "dice"` — not this port's
  addition, already there when this feature was picked up), given the same
  semantics as `/d<N>`, since it has no existing v1 behaviour to preserve or
  conflict with.
- Maps QA's bare `roll` (no argument) to the same `dice_exemple` text v1's own
  `/dado`/`/dice` show — the closest real v1 string to "you must specify the
  number of sides", satisfying that scenario without inventing new copy.

### The full argument-handling table

| Input | v1 (`/d<N>` shorthand) | v1 (`/dado`, `/dice`) | v2 (`/d<N>`, `roll <N>`) |
|---|---|---|---|
| No argument | `vezes = 1` | Always shows the example, ignores any argument entirely | `times = 1` |
| Non-numeric second argument | Uncaught `ValueError`, silent (see above) | N/A — example shown regardless | `dice_exemple` reply (fixed, not silent) |
| `0` sides (`/d0`) | Dispatches (`"0".isdigit()` is `True`), then uncaught `ValueError` in `random.randint(1, 0)`, silent | N/A | `dice_exemple` reply (fixed, not silent) |
| Negative sides | Unreachable via `/d<N>` (dispatcher's own `.isdigit()` check rejects a `-`); reachable via v2-only `roll -5` | N/A | `dice_exemple` reply (this port's own new-trigger path; no v1 precedent to diverge from) |
| Absurdly large sides (e.g. `999999999`) | Accepted — Python ints are unbounded, `random.randint` succeeds | N/A | Accepted via `roll <N>` (no cap added); **not** reachable via `/d<N>` in v2 — see the textmatch gap below |
| Repeat count `> 20` | Clamped to `20` | N/A | Clamped to `20` (same formula, `max(min(20, n), 1)`) |
| Repeat count `<= 0` | Clamped to `1` | N/A | Clamped to `1` |
| Several arguments (`/d20 5 hello world`) | Only `split()[1]` is ever read; everything past it is silently ignored, not an error | Still shows the example | Same: only the second token is read as `times`, the rest ignored |
| `/dado`, `/dice` with any argument | Always the example | (same row) | Always the example (`parse_invocation` returns `None` whenever the head is `dado`/`dice`, unconditionally) |

## Phase 3 — QA scenario

Copied `../Cookiebot-QA/features/fun_dice.feature` verbatim into
`qa/features/fun_dice.feature` (all three original scenarios, unchanged
wording), then added: the `/dado`/`/dice` always-shows-the-example quirk (bare
and with a trailing argument), the `/d<N>` shorthand actually rolling, the
repeat-count cap, the non-numeric-argument and zero-sides fixes, the
`functions_utility` gate notice, and a command addressed at a different bot
being ignored (mirrors the pattern established in `core_privacy.feature`).

## Implementation (v2)

`packages/cb-gateway/src/cb_gateway/handlers/dice.py`:

- `router.message(CommandName("dice"))` — `CommandName` resolves `dice`,
  `dado`, `roll` and the `/d<N>` regex shorthand, all already in
  `cb_core/textmatch.py:COMMAND_ALIASES`/`_DICE_SHORTHAND` before this port
  started.
- `head_word(parsed.raw)` recovers which literal alias fired (`ParsedCommand`
  only carries the canonical `"dice"` name) — mirrors `parse_command`'s own
  internal head derivation rather than importing its private regex.
- `parse_invocation` is the pure function behind every row of the argument
  table above; `None` means "reply with the usage example", covering both
  v1's real always-the-example branch and this port's own fixed crash paths.
- `render_roll` is a byte-for-byte port of v1's `resposta` string-building,
  including the English catalog's literal "Nth Roll" grammar quirk.
- `roll` uses `random.randint(1, sides)` — not `secrets` — matching v1's
  actual (non-cryptographic) distribution.
- `ctx.enabled("utility")` checked inline, replying with `t(ctx, "utility_off")`
  on the way out, not `FeatureGate("utility")` — see the corrections above.
- Usage-example deliveries are `message.answer(...)` (a send); an actual roll
  result and the `utility_off` notice are `message.reply(...)` — matches v1's
  own send/reply split exactly (see the Phase 2 table).

## Phase 6 — Parity table

| Row | Verdict | Note |
|---|---|---|
| Triggers `/dado`, `/dice` | same | via existing `COMMAND_ALIASES` + `CommandName("dice")`. |
| Trigger `/d<N>` (1-4 digits) | same | via existing `_DICE_SHORTHAND`. |
| Trigger `/d<N>` beyond 4 digits (e.g. `/d99999`) | changed (unintentional — a real gap, not fixed by this port) | `_DICE_SHORTHAND` caps at `\d{1,4}` (max `9999`); v1's own dispatcher condition has no such cap. `/d99999` does not even parse as a command in v2 (`parse_command` returns `None`). `cb_core/textmatch.py` is not this port's file to edit — flagged here and in the final report for whoever owns it. |
| Trigger `roll <N> [times]` | changed (intentional, new) | Has no v1 equivalent at all (FEATURE-MAP's "spec/code trigger mismatch"); given `/d<N>`'s exact semantics rather than left undefined, since QA's own scenarios require it to behave sensibly. |
| Gate: `functions_utility`, not `functions_fun` | changed (intentional, fix of a wrong assumption) | Verified against `COOKIEBOT.py:248-255` directly; matches `group_config.py`'s and `filters.py`'s own docstrings. |
| Gate notice replies (not silently drops) | same | `ctx.enabled("utility")` checked inline + `message.reply(t(ctx, "utility_off"))`, matching v1's `notify_utility_off` reply — not `FeatureGate`, whose own docstring describes the wrong (silent) shape for this command family. |
| `/dado`/`/dice` always show the example, regardless of arguments | same | preserved exactly, including with a trailing argument (`/dado 6` still shows the example, does not roll). |
| `/dado`/`/dice` usage text is a send, not a reply | same | `message.answer(...)`. |
| Roll result is a reply | same | `message.reply(...)`. |
| Repeat count clamped to `[1, 20]` | same | identical formula. |
| Extra arguments beyond the second are ignored | same | preserved as a harmless quirk, not an error. |
| Non-numeric argument crashes silently | changed (intentional, fix) | Silent-failure bug per the migrate-feature skill's own guidance ("race conditions and silent-failure bugs get fixed"); now replies with the usage example instead of going silent. |
| Zero sides (`/d0`) crashes silently | changed (intentional, fix) | Same fix, same reasoning. |
| Negative/absurdly-large sides via `roll <N>` | changed (intentional; no v1 precedent to preserve) | Negative replies with the usage example (this port's own validation); absurdly large is accepted with no cap, matching v1's actual tolerance for `/d<N>` (Python ints are unbounded) on the one trigger spelling that can still reach it in v2. |
| Reply text (English `dice_roll` "Nth Roll" grammar quirk) | same | preserved verbatim, not corrected to "1st/2nd/3rd". |
| Localised strings (en/pt) | same | byte-identical catalog values. |
| Localised strings (es) | same (both v1 and v2 fall back to en) | v1's own `es/lib.json` is missing `dice_roll`/`dice_exemple` too — `cb_core.locales.get`'s en-fallback reproduces v1's `Localizer.bundle()` deep-merge-under-default-language behaviour exactly, not a v2-only gap. |
| `send_chat_action('typing')` | changed (intentional, drop) | cosmetic-only; no other ported handler in this codebase sends one (see `core_privacy.md`, `core_rules.md`). |
| Persistence | same | none. |
| Cooldown/quota | same | none. |

## Known gaps for whoever owns the listed files

- `packages/cb-gateway/src/cb_gateway/handlers/__init__.py` does not import or
  register `dice.router` — needs `root.include_router(dice.router)` (plus the
  import) for `qa/test_fun_dice.py` to pass end to end. Out of this port's
  file ownership; the same gap is already flagged in `core_rules.md` and
  `fun_random.md` for their own handlers.
- `cb_core/textmatch.py`'s `_DICE_SHORTHAND` regex caps `/d<N>` at 4 digits;
  v1 has no such cap. A real, if minor, behaviour regression for anyone typing
  a 5+ digit `/d<N>` command. Not this port's file to edit.
- `docs/site/content/docs/feature-map.mdx`'s `fun_dice` row could use a note pointing at the
  `functions_utility` (not `functions_fun`) gate correction above and at the
  `/d99999` gap; this agent could not edit that file (out of scope for this task).
