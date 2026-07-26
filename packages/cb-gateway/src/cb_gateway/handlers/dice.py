"""fun_dice — `/dado`, `/dice`, the `/d<N>` shorthand, and QA's `roll` spelling.

v1: `roll_dice`, `../COOKIEBOT-Telegram-Group-Bot/Bot/Miscellaneous.py:160-183`::

    def roll_dice(cookiebot, msg, chat_id, language):
        send_chat_action(cookiebot, chat_id, 'typing')
        start = msg['text'].split(" ")[0]
        if start in ("/dado", "/dice"):
            text = i18n.get("dice_exemple", lang=language)
            send_message(cookiebot, chat_id, text)
        else:
            if len(msg['text'].split()) == 1:
                vezes = 1
            else:
                vezes = int(msg['text'].replace("@CookieMWbot", '').replace("@pawstralbot", '').split()[1])
                vezes = max(min(20, vezes), 1)
            limite = int(msg['text'].replace("@CookieMWbot", '').replace("@pawstralbot", '').split()[0][2:])
            resposta = f"(d{limite}) "
            if vezes == 1:
                resposta += f"🎲 -> {random.randint(1, limite)}"
            else:
                for vez in range(vezes):
                    ctx = {"vez": vez + 1, "roll": random.randint(1, limite)}
                    resposta += i18n.get("dice_roll", lang=language, **ctx)
            send_message(cookiebot, chat_id, resposta, msg_to_reply=msg)

Dispatched from `../COOKIEBOT-Telegram-Group-Bot/Bot/COOKIEBOT.py:248-255`::

    elif msg['text'].startswith(("/dado", "/dice", "/patas", "/bff", ...)) or \
            (msg['text'].startswith("/d") and msg['text'].split()[0].split('/d')[1].isdigit()):
        if msg['text'].startswith(("/patas", "/bff", ...)):
            event_countdown(...)
        elif not utilityfunctions:
            notify_utility_off(cookiebot, msg, chat_id, language)
        elif msg['text'].startswith(("/dado", "/dice", "/d")):
            roll_dice(cookiebot, msg, chat_id, language)
        ...

Full Phase 2/6 contract: `docs/contracts/fun_dice.md`. QA: was
`../Cookiebot-QA/features/fun_dice.feature` (spec/code trigger mismatch, see
`docs/FEATURE-MAP.md`'s `fun_dice` row), ported to `qa/features/fun_dice.feature`.

## The two things this docstring corrects about the task brief

1. **Gate.** Dice is *not* gated by `functionsFun`. It sits in the second
   `elif` chain of the dispatcher (`COOKIEBOT.py:248-255`), which checks
   `utilityfunctions`, not the first chain (`:214-217`) that checks
   `funfunctions`. `cb_core/group_config.py`'s own `_FEATURE_AREAS` docstring
   and `cb_gateway/filters.py`'s `FeatureGate` docstring already say as much
   ("COOKIEBOT.py:218,252" — 218 is fun, 252 is utility). This handler uses
   `ctx.enabled("utility")`, not `"fun"`.
2. **`FeatureGate` itself is the wrong tool here anyway.** Its docstring
   claims a gated-off command in v1 "simply is not dispatched — no error, no
   reply". That is true for some commands but not this one: v1's dispatcher
   explicitly calls `notify_utility_off`, which *does* reply (`send_message`'s
   4th positional argument is `msg_to_reply`). So this handler checks
   `ctx.enabled("utility")` itself and replies with the `utility_off` catalog
   string on the way out, the same pattern `fun_random.py` already established
   for its own (`"fun"`/`fun_off`) gate — see that module's docstring.

## Trigger shapes and what each one does

`cb_core/textmatch.py:COMMAND_ALIASES` maps three literal words to the
canonical command name `"dice"` — `dice`, `dado`, `roll` — plus a regex
shorthand for `/d<N>` (1-4 digits). `ParsedCommand` only carries the
*canonical* name, so `_head_word` below recovers which literal alias fired
from `parsed.raw`, mirroring `parse_command`'s own internal head derivation
(not importing it — that constant is private to `textmatch.py`, which is not
this port's file to touch).

- `/dado`, `/dice` (any trailing text, any bot suffix) -> **always** shows the
  usage example, verbatim, regardless of arguments. This is a genuine v1
  quirk, not a bug: `start = msg['text'].split(" ")[0]` only ever compares the
  *first* whitespace-separated token against `("/dado", "/dice")` — trailing
  text never changes which branch runs. `/dado 6` shows the example; it does
  not roll a d6. Preserved exactly.
- `/d<N>` (`N` 1-4 digits, e.g. `/d20`) -> rolls an N-sided die. An optional
  second token is the repeat count ("vezes"), clamped to `[1, 20]`
  (`max(min(20, vezes), 1)`, `Miscellaneous.py:171`) exactly as v1 does.
- `roll <N> [times]` -> **not a v1 trigger at all** (FEATURE-MAP: "spec/code
  trigger mismatch" — QA's `roll 6` has no v1 equivalent). Since this is a net
  new alias rather than a replacement for an existing one (AGENTS.md §2.1),
  it is free to get sensible new-command semantics: it behaves exactly like
  `/d<N>` (`../Cookiebot-QA/features/fun_dice.feature`'s three scenarios pass
  because of this, not because v1 ever spoke this dialect). `roll` with no
  argument shows the usage example — QA's own third scenario asks for "an
  error message indicating that the number of sides must be specified", and
  the closest real v1 string for that is the same usage example `/dado`/`/dice`
  already show.

## Silent-failure bugs fixed, not preserved

v1's `int(...)` calls at `Miscellaneous.py:170,172` are bare — a non-numeric
"vezes", or a sides value that parses to `0` (`"0".isdigit()` is `True`, so
`/d0` *does* dispatch), reaches `random.randint(1, 0)` uncaught. Every message
handler in v1 runs inside a bare `except Exception:` (`COOKIEBOT.py:329,432`)
that only prints and moves on — the user sees nothing at all. Per the
migrate-feature skill ("race conditions and silent-failure bugs get fixed;
user-visible quirks are usually preserved"), this port never lets a bad
argument crash silently: any parse failure or non-positive `sides` falls back
to the same usage-example reply instead. `docs/contracts/fun_dice.md`'s parity
table calls this out row by row.

## Reply vs send

v1's `roll_dice` sends the usage example with `send_message(..., text)` — no
`msg_to_reply`, i.e. a plain send into the chat, not a reply. The actual roll
result uses `send_message(..., resposta, msg_to_reply=msg)` — a reply. This
port keeps that split: every usage-example delivery (whether from a bare
`/dado`/`/dice`/`roll`, or this port's own parse-failure fallback) is
`message.answer(...)`; an actual roll result, and the `utility_off` gate
notice, are `message.reply(...)`.

## Randomness

`random.randint(1, sides)`, matching v1's `random.randint(1, limite)`
(`Miscellaneous.py:175,180`) — not `secrets`. v1's dice rolls have never been
cryptographically meaningful and swapping the RNG family is not a "port", it
is a different feature that happens to look the same.

## A gap this port cannot close

`cb_core/textmatch.py`'s `_DICE_SHORTHAND` regex caps the `/d<N>` shorthand at
1-4 digits (`^d(\\d{1,4})$`, max `9999`). v1's own dispatcher condition
(`msg['text'].split()[0].split('/d')[1].isdigit()`) has no such cap — a huge
`/d<N>` (e.g. `/d999999999`) dispatches fine in v1 and rolls a huge number
successfully (Python ints have no size limit). In v2, `/d99999` (5 digits)
does not even parse as a command (`parse_command` returns `None`) — a real,
observable behaviour regression from v1 for that one trigger shape. This
port's own `roll <N>` path has no such cap (`parse_invocation` below places no
upper bound on `sides` beyond what `int()` itself accepts), so the QA-facing
"absurdly large number" case is covered through that spelling; the `/d<N>`
cap is `textmatch.py`'s to lift, out of this file's ownership — flagged in
`docs/contracts/fun_dice.md` and in this port's final report.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from typing import cast

from aiogram import Bot, Router
from aiogram.types import Message

from cb_core import locales
from cb_core.textmatch import ParsedCommand
from cb_gateway.context import context_for, t
from cb_gateway.filters import CommandName
from cb_gateway.telemetry import mark_outcome

router = Router(name="dice")

# Mirrors the shape of cb_core.textmatch._DICE_SHORTHAND (private to that
# module, not imported) -- used here only to tell /d<N> apart from a bare
# /dado, /dice or roll invocation once CommandName has already resolved all
# four to the canonical "dice" name.
_SHORTHAND_HEAD = re.compile(r"^d(\d{1,4})$")

_MIN_SIDES = 1
_MIN_TIMES = 1
_MAX_TIMES = 20  # Miscellaneous.py:171: vezes = max(min(20, vezes), 1)


@dataclass(frozen=True, slots=True)
class RollRequest:
    sides: int
    times: int


def head_word(raw: str) -> str:
    """The literal alias the user typed: `"dado"`, `"dice"`, `"roll"` or `"d20"`.

    `ParsedCommand.name` is always the canonical `"dice"`; `.raw` is the
    original text, from which this recovers the actual head token the same way
    `parse_command` derives its own `head` internally (`textmatch.py:118-131`).
    `raw` is guaranteed to start with `/` -- `parse_command` never returns a
    `ParsedCommand` otherwise.
    """
    body = raw[1:]
    end = 0
    while end < len(body) and not body[end].isspace():
        end += 1
    head = body[:end]
    at = head.find("@")
    if at >= 0:
        head = head[:at]
    return head.lower()


def _parse_int(token: str) -> int | None:
    try:
        return int(token)
    except ValueError:
        return None


def parse_invocation(parsed: ParsedCommand) -> RollRequest | None:
    """`None` means "reply with the usage example" -- covers v1's literal
    `/dado`/`/dice` branch (always the example, regardless of arguments) and
    every argument-parsing failure this port declines to let crash (see the
    module docstring's "silent-failure bugs fixed" section).
    """
    head = head_word(parsed.raw)
    tokens = parsed.args.split()

    sides: int | None
    shorthand = _SHORTHAND_HEAD.match(head)
    if shorthand is not None:
        sides = int(shorthand.group(1))  # regex-guaranteed digits: always valid
        remaining = tokens[1:]  # tokens[0] just echoes `sides` (textmatch.py:142)
    elif head == "roll":
        if not tokens:
            return None
        sides = _parse_int(tokens[0])
        if sides is None:
            return None
        remaining = tokens[1:]
    else:
        # "dado" / "dice" -- and, defensively, anything else CommandName("dice")
        # could ever let through (unreachable given COMMAND_ALIASES today).
        return None

    if sides < _MIN_SIDES:
        return None  # v1's silent crash for /d0 and friends, fixed (see docstring)

    times = _MIN_TIMES
    if remaining:
        parsed_times = _parse_int(remaining[0])
        if parsed_times is None:
            return None
        times = max(min(_MAX_TIMES, parsed_times), _MIN_TIMES)

    return RollRequest(sides=sides, times=times)


def render_roll(sides: int, rolls: list[int], lang: str) -> str:
    """Byte-for-byte port of v1's `resposta` building (`Miscellaneous.py:173-182`).

    A single roll skips the `dice_roll` catalog entirely: `"(d{sides}) 🎲 -> {roll}"`.
    Several rolls prefix with the same `"(d{sides}) "` and then append one
    `dice_roll`-formatted line per roll -- that catalog string itself starts
    with `"\\n"`, and (English) always says "Nth Roll" with a literal "th" for
    every N, a real v1 grammar quirk preserved rather than fixed.
    """
    if len(rolls) == 1:
        return f"(d{sides}) \U0001f3b2 -> {rolls[0]}"
    body = f"(d{sides}) "
    for vez, roll in enumerate(rolls, start=1):
        body += locales.get("dice_roll", lang, vez=vez, roll=roll)
    return body


def roll(request: RollRequest) -> list[int]:
    """`random.randint(1, sides)`, `times` times -- matches v1's distribution
    exactly (see the module docstring's "Randomness" section)."""
    return [random.randint(1, request.sides) for _ in range(request.times)]


@router.message(CommandName("dice"))
async def roll_dice(message: Message, parsed: ParsedCommand | None = None) -> None:
    """`/dado`, `/dice`, `/d<N>` and `roll <N>` -- see the module docstring for
    the full trigger/behaviour breakdown and `docs/contracts/fun_dice.md` for
    the Phase 2/6 contract.
    """
    if parsed is None:  # pragma: no cover - CommandName always injects a match
        return

    ctx = await context_for(cast(Bot, message.bot), message)
    if not ctx.enabled("utility"):
        # v1: notify_utility_off (Miscellaneous.py:133-135) replies -- its
        # send_message call passes `msg` positionally into `msg_to_reply`.
        mark_outcome("refused")
        await message.reply(t(ctx, "utility_off"))
        return

    request = parse_invocation(parsed)
    if request is None:
        # v1: send_message(cookiebot, chat_id, text) -- no msg_to_reply, a send.
        await message.answer(t(ctx, "dice_exemple"))
        return

    rolls = roll(request)
    await message.reply(render_roll(request.sides, rolls, ctx.lang))


__all__ = ["RollRequest", "head_word", "parse_invocation", "render_roll", "roll", "router"]
