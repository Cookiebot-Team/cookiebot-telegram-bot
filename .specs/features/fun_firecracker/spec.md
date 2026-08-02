# fun_firecracker — Specify

**Feature id:** `fun_firecracker` · **Milestone:** M2 · **Kind:** v1 port
**v1 source:** `Bot/Miscellaneous.py:226-238`, dispatched `Bot/COOKIEBOT.py:215,230-231`

## Goal

A user types `/rojao` (or one of four aliases) and the bot fires off a
firecracker: a reaction, a fuse, a random-length burst of `pra ` messages, and a
bang. Pure text, no persistence, no external service. The cheapest remaining M2
port and the one that establishes how a multi-message reply sequence is written
and tested in v2.

## Scope

In: the handler, its router registration, unit + integration + acceptance tests,
the behaviour contract, the status flip.
Out: localisation of the three output strings (v1 never localised them — see
D-FC-1), any cooldown (v1 has none), any new infrastructure.

## Behaviour contract (Phase 2)

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
| D-FC-1 | Never localised: `firecracker()` takes no `language` argument, so `send_message` falls through on its `language="pt"` default and `translate()` never fires (`universal_funcs.py:195-198`). Output is byte-identical in every language. | **preserve.** The three strings are onomatopoeia, not words; localising them changes observable output for every existing group. Record it in the contract. |
| D-FC-2 | Flood risk: up to ~21 sends with no throttle between them. | **preserve the sequence, bound the risk.** Same message count and same 0.1s pre-loop pause, but the sequence must not block other updates (see design R1.4). Do not add per-message sleeps — that changes timing users already know. |

## QA scenario

`Cookiebot-QA/features/fun_firecracker.feature` exists, one scenario:

```gherkin
Feature: sends a firecracker message sequence to the group when the user types a specific command

    Background:
        Given that the bot is in the group and properly set up

    Scenario: User types the firecracker command
        Given that the user is a member of the group
        When the user types the command "/firecracker"
        Then the bot should send multiple firecracker messages in a sequence to the group
```

No QA/v1 conflict: "multiple … in a sequence" matches v1's variable-length loop.
The spec has no gate-off scenario — add one, per AGENTS.md §6 ("write the
scenario as part of the port").

## Success criteria

1. All five triggers resolve, with and without a trailing argument and with
   `@botname`.
2. The emitted sequence is exactly ①–⑤ above, with the three strings byte-identical.
3. `functionsFun` off ⇒ exactly one message, the localised `fun_off` text.
4. Unit, integration and acceptance tests green; `docs/contracts/fun_firecracker.md`
   carries the Phase-2 and Phase-6 tables.
