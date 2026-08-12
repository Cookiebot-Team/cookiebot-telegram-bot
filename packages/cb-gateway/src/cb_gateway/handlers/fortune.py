"""x_fortune_cookie — `/sorte`, `/fortunecookie`, `/suerte`: an animated
fortune cookie plus six lucky numbers.

v1: `fortune_cookie`, `../COOKIEBOT-Telegram-Group-Bot/Bot/Miscellaneous.py:359-375`::

    def fortune_cookie(cookiebot, msg, chat_id, language):
        send_chat_action(cookiebot, chat_id, 'upload_photo')
        anim_id = send_animation(cookiebot, chat_id, 'https://s12.gifyu.com/images/S5e9b.gif', msg_to_reply=msg)
        line = i18n.get_random_line("sorte.txt", lang=language)
        numbers = []
        tens = []
        while len(numbers) < 6:
            number = random.randint(1, 99)
            if math.floor(number / 10) not in tens:
                numbers.append(number)
                tens.append(math.floor(number / 10))
        numbers_str = ' '.join([str(number) for number in numbers])
        answer = i18n.get("luck", lang=language, line=line, num=numbers_str)
        time.sleep(3)
        delete_message(cookiebot, (str(chat_id), str(anim_id)))
        send_chat_action(cookiebot, chat_id, 'typing')
        send_message(cookiebot, chat_id, answer, msg_to_reply=msg, parse_mode='HTML')

Dispatched at `COOKIEBOT.py:240-241`, the same `funfunctions`-gated block as
`age.py`/`gender.py` (see `age.py`'s module docstring for the shared
`fun_off` shape — this handler checks the gate itself and replies, rather
than the silent `FeatureGate` filter).

## The lucky-number rule (preserved exactly)

Six numbers in `1..99`, at most one per "tens" decade
(`floor(n/10)` distinct across all six — `pick_lucky_numbers` below). There
are 10 possible decades (`0` for 1-9, ..., `9` for 90-99) and only 6 are ever
needed, so this terminates with probability 1 by the same birthday-paradox
argument v1's own unbounded `while` loop relied on without stating it: each
draw has at least a 4-in-10 chance of landing in an unused decade once five
are taken, so the expected number of draws to finish is small and bounded,
never literally infinite. No attempt cap is added (contrast `unearth.py`'s
bounded retry) because each draw is pure in-process arithmetic, not a network
call — there is no cost to bound against.

## Deviations from v1, and why

1. **The 3-second hold is deferred, not blocking.** v1's `time.sleep(3)`
   between sending the animation and deleting it blocks the whole process —
   every group, every handler — for three seconds (AGENTS.md's "nothing slow
   on the reply path" non-negotiable is aimed exactly at this). This port
   sends the animation, then schedules the delete-then-answer tail on
   `asyncio.create_task` and returns immediately, the exact idiom
   `complaint.py`'s `_schedule_tail`/`_delayed_reveal` and
   `groupguardian.py`'s `_schedule_unban` already established for a
   V1 blocking-sleep-turned-background-task. `_DELETE_DELAY` (a module
   constant, not a literal) keeps v1's exact user-visible order — the
   animation disappears, *then* the fortune text with the lucky numbers
   arrives — while letting the gateway's own request/response cycle finish
   immediately rather than holding an update open for three seconds. Restart
   caveat: still an in-process task, so a gateway restart inside the window
   loses the scheduled deletion and answer, same documented gap
   `groupguardian.py`/`complaint.py` already carry (no gateway->worker
   enqueue wiring yet).
2. **`sorte.txt` is already shipped and read via `locales.lines("sorte", lang)`**
   (`cb_core/locale_data/{en,pt,es}/sorte.txt`, `locales.py`'s `_LINE_FILES`) —
   v1's own `i18n.get_random_line("sorte.txt", ...)` is the same file, minus
   the `.txt` suffix `locales.lines()` already strips for every line-list.
   No new asset, no new catalog key.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
from typing import Final, cast

from aiogram import Bot, Router
from aiogram.types import Message

from cb_core import locales
from cb_gateway.context import context_for, deny_if_disabled, t
from cb_gateway.filters import CommandName

router = Router(name="fortune")

# Miscellaneous.py:361 — v1's hardcoded gifyu animation, unchanged.
_ANIMATION_URL = "https://s12.gifyu.com/images/S5e9b.gif"

# Miscellaneous.py:372 — v1's `time.sleep(3)`, now the delay before the
# deferred tail runs (module docstring, deviation 1).
_DELETE_DELAY: Final = 3

# One shared rng for both the fortune line and the lucky numbers, same idiom
# as fun_complaint's module-level `_rng` (plain `random`, not `secrets` —
# nothing here is security-sensitive).
_rng = random.Random()

# The tail's `asyncio.sleep` as a module attribute so a test can monkeypatch
# it to an instant no-op instead of actually waiting (mirrors
# `complaint.py`'s `_sleep` seam exactly).
_sleep = asyncio.sleep

# A bare `asyncio.create_task(...)` is only weakly referenced by the event
# loop and can be garbage-collected mid-sleep. Held here until each task
# finishes — the same idiom `complaint.py`'s `_pending_tails` and
# `groupguardian.py`'s `_pending_unbans` use.
_pending_tails: set[asyncio.Task[None]] = set()


def pick_lucky_numbers(rng: random.Random | None = None) -> list[int]:
    """Six numbers in `1..99`, at most one per tens-decade (see module
    docstring). A pure function of the rng so the distinct-decade invariant
    is testable without a Bot, a chat, or a network.
    """
    chooser = rng or random
    numbers: list[int] = []
    decades: set[int] = set()
    while len(numbers) < 6:
        number = chooser.randint(1, 99)
        decade = number // 10
        if decade not in decades:
            numbers.append(number)
            decades.add(decade)
    return numbers


@router.message(CommandName("fortune"))
async def fortune_cookie(message: Message) -> None:
    bot = cast(Bot, message.bot)
    ctx = await context_for(bot, message)
    if await deny_if_disabled(message, ctx, "fun"):
        return

    await bot.send_chat_action(message.chat.id, "upload_photo")
    animation_message = await message.reply_animation(_ANIMATION_URL)

    line = _rng.choice(locales.lines("sorte", ctx.lang))
    numbers = pick_lucky_numbers(_rng)
    numbers_str = " ".join(str(number) for number in numbers)
    answer = t(ctx, "luck", line=line, num=numbers_str)

    _schedule_tail(bot, message, animation_message.message_id, answer)


def _schedule_tail(bot: Bot, message: Message, animation_message_id: int, answer: str) -> None:
    """Fires the delete-then-answer tail off the reply path (module
    docstring, deviation 1)."""
    task = asyncio.create_task(_delayed_tail(bot, message, animation_message_id, answer))
    _pending_tails.add(task)
    task.add_done_callback(_pending_tails.discard)


async def _delayed_tail(bot: Bot, message: Message, animation_message_id: int, answer: str) -> None:
    """v1: `sleep(3)` -> delete the animation -> `send_chat_action('typing')`
    -> the fortune text, `parse_mode='HTML'` (`Miscellaneous.py:372-375`)."""
    await _sleep(_DELETE_DELAY)
    # v1's own `delete_message` swallows its own errors
    # (`universal_funcs.py:340-344`) — the animation may already be gone.
    with contextlib.suppress(Exception):
        await bot.delete_message(message.chat.id, animation_message_id)
    await bot.send_chat_action(message.chat.id, "typing")
    await message.reply(answer, parse_mode="HTML")


__all__ = ["fortune_cookie", "pick_lucky_numbers", "router"]
