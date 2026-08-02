"""fun_firecracker — `/rojao`, `/rojão`, `/acende`, `/fogos`, `/firecracker`.

v1: `firecracker`, `../COOKIEBOT-Telegram-Group-Bot/Bot/Miscellaneous.py:226-238`::

    def firecracker(cookiebot, msg, chat_id):
        react_to_message(msg, '\U0001f389', is_big=True)
        send_message(cookiebot, chat_id, "fiiiiiiii.... ", msg_to_reply=msg)
        time.sleep(0.1)
        amount = random.randint(5, 20)
        while amount > 0:
            n = random.randint(1, amount) if random.random() < 0.5 else 1
            send_message(cookiebot, chat_id, "pra " * n)
            amount -= n
        send_message(cookiebot, chat_id, "<b> \U0001f4a5POOOOOOOWW\U0001f4a5 </b>")

Dispatched from `COOKIEBOT.py:215,230-231`: the alias tuple lives at `:215` and
the `startswith` prefix match at `:230-231`, gated on `funfunctions` the same
way `fun_ship`'s `/shippar`/`/ship` arm is (`COOKIEBOT.py:218-219`) — a
gated-off group is *told*, not ignored (`notify_fun_off`, `Miscellaneous.py:129-131`).

Contract: `docs/contracts/fun_firecracker.md`. QA:
`../Cookiebot-QA/features/fun_firecracker.feature` -> `qa/features/fun_firecracker.feature`.

## Trigger

`cb_core/textmatch.py:47-48` already maps all five spellings — `firecracker`,
`rojao`, `rojão`, `acende`, `fogos` — to the canonical `"firecracker"` command;
`CommandName("firecracker")` is the whole filter, no new alias work here
(design R1.3).

## Shape, copied from `fun_ship`

Same fun-gate semantics (a reply, not silence), same
`contextlib.suppress(Exception)` around the reaction, same `mark_outcome` call
on the refused path, same module-level `random` idiom — `ship.py` calls
`random` directly rather than threading a `random.Random` instance through the
module, so this port matches that rather than inventing a second convention
(design R2.1). The one deliberately pure piece is `burst()`, so the message
count and amount-drawn maths are unit-testable without a `Message` or a `Bot`.

## Output fidelity (design R4)

The three literal strings are v1's, byte-identical, and never localised
(D-FC-1, `spec.md`) — see the per-constant comments below for the exact
`Miscellaneous.py` line each one came from. This bot's `Bot` instance is
constructed with `parse_mode=HTML` as its default (see `welcome.py`'s module
docstring), and none of `message.react`/`message.reply`/`message.answer` below
overrides that, so the bang's `<b>...</b>` renders bold exactly as v1's single
shared `send_message` call did — no explicit `parse_mode=` needed on any of
the three sends (design R4.2).

`await asyncio.sleep(0.1)` reproduces v1's `time.sleep(0.1)` between the fuse
and the burst (`Miscellaneous.py:229`, design R4.4) without blocking the event
loop; nothing sleeps between burst lines (design R4.4, D-FC-2 — the flood risk
is preserved, not throttled further, per spec.md's D-FC-2 verdict). aiogram
dispatches each update in its own task, so this multi-second, multi-send
sequence never blocks other chats' updates (design R1.4) — no worker job.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
from typing import Final, cast

from aiogram import Bot, Router
from aiogram.types import Message, ReactionTypeEmoji

from cb_core.textmatch import ParsedCommand
from cb_gateway.context import context_for, t
from cb_gateway.filters import CommandName
from cb_gateway.telemetry import mark_outcome

router = Router(name="firecracker")

# Miscellaneous.py:228 -- the fuse line, a reply to the trigger message.
# D-FC-1: onomatopoeia, deliberately never run through the locale catalog in
# v1 (`firecracker()` takes no `language` argument at all) -- preserved as-is
# per spec.md, not "fixed" into a translated string.
_FUSE: Final = "fiiiiiiii.... "

# Miscellaneous.py:236 -- repeated `n` times per burst line, built by burst().
# Same D-FC-1 note: no locale catalog involved, ever.
_PRA: Final = "pra "

# Miscellaneous.py:238 -- the final bang, HTML markup and all. Same D-FC-1 note.
_BANG: Final = "<b> \U0001f4a5POOOOOOOWW\U0001f4a5 </b>"

# Miscellaneous.py:230 -- amount = random.randint(5, 20)
_MIN_AMOUNT: Final = 5
_MAX_AMOUNT: Final = 20


def burst(rng: random.Random | None = None) -> list[str]:
    """The `"pra "`-repeat lines only -- not the fuse or the bang.

    Byte-for-byte port of `Miscellaneous.py:230-236`::

        amount = random.randint(5, 20)
        while amount > 0:
            n = random.randint(1, amount) if random.random() < 0.5 else 1
            send_message(cookiebot, chat_id, "pra " * n)
            amount -= n

    A coin flip per iteration either draws `n` uniformly from what remains or
    spends exactly one, so the loop always terminates and the total `"pra "`
    count across every returned line always equals the `amount` drawn --
    design R2.2's invariants, asserted in `test_firecracker.py` over a seeded
    `rng` and over 1000 seeds.

    `rng` defaults to `None` rather than a module-level `_rng` instance: like
    `ship.py` and `dice.py`, this codebase's rng idiom is to call the `random`
    module's functions directly (design R2.1), so the production call site
    below passes nothing and gets that same shared state; tests pass a seeded
    `random.Random` to make the draw reproducible.
    """
    draw_int = rng.randint if rng is not None else random.randint
    draw_float = rng.random if rng is not None else random.random
    amount = draw_int(_MIN_AMOUNT, _MAX_AMOUNT)
    lines: list[str] = []
    while amount > 0:
        n = draw_int(1, amount) if draw_float() < 0.5 else 1
        lines.append(_PRA * n)
        amount -= n
    return lines


@router.message(CommandName("firecracker"))
async def rojao(message: Message, parsed: ParsedCommand | None = None) -> None:
    """`/rojao`, `/rojão`, `/acende`, `/fogos`, `/firecracker` -- see the module
    docstring for the full sequence and `docs/contracts/fun_firecracker.md` for
    the Phase 2/6 contract."""
    if parsed is None:  # pragma: no cover - CommandName always injects a match
        return

    ctx = await context_for(cast(Bot, message.bot), message)
    if not ctx.enabled("fun"):
        # v1: notify_fun_off passes `msg` positionally into send_message's
        # `msg_to_reply` (Miscellaneous.py:130) -- a reply, not a bare send.
        # Gate first and only: disabled means this one reply and nothing else.
        mark_outcome("refused")
        await message.reply(t(ctx, "fun_off"))
        return

    # Best-effort, like every other reaction in this codebase: v1's own
    # react_to_message has no error handling and its caller swallows
    # everything (COOKIEBOT.py:329).
    with contextlib.suppress(Exception):
        await message.react(reaction=[ReactionTypeEmoji(emoji="\U0001f389")], is_big=True)

    await message.reply(_FUSE)
    await asyncio.sleep(0.1)  # Miscellaneous.py:229 -- time.sleep(0.1)

    for line in burst():
        await message.answer(line)

    await message.answer(_BANG)


__all__ = ["burst", "rojao", "router"]
