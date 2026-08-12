"""x_unearth — `/desenterrar`, `/unearth`: forward a random earlier message from
this same group back into the chat.

v1: `unearth`, `../COOKIEBOT-Telegram-Group-Bot/Bot/Miscellaneous.py:325-333`::

    def unearth(cookiebot, msg, chat_id, thread_id=None):
        send_chat_action(cookiebot, chat_id, 'typing')
        for _ in range(100):
            try:
                chosenid = random.randint(1, msg['message_id'])
                forward_message(cookiebot, chat_id, chat_id, chosenid, thread_id=thread_id)
                return chosenid
            except Exception:
                return None

Dispatched at `COOKIEBOT.py:236-237`, inside the `funfunctions`-gated block
whose `else` answers with the `fun_off` text (`:218-219`) — so this handler
checks the gate itself and replies, the pattern `fun_random.py` established,
rather than using the silent `FeatureGate` filter.

There is no stored pool here and there does not need to be one: a group's
message ids are dense integers from 1, so "a random earlier message" is a
random integer below the command's own id, and Telegram itself is the storage.
That also means a chosen id can be a deleted message, a service message, or a
message the bot may not forward — misses are normal and expected, not errors.

## The retry v1 wrote and then disabled

`for _ in range(100)` reads as "keep trying until one forwards", and the body
`return None`s inside the `except`, so the loop can never run twice: v1 makes
exactly one attempt and answers nothing whenever that attempt misses. On a
group with any deletion history that is most of the time, which is why the
command has a reputation for doing nothing.

This port makes the retry real, bounded at `_ATTEMPTS`. It is the one place
here that does not reproduce v1 byte for byte, and the choice is deliberate:
the alternative is porting a loop whose only observable effect is the one
iteration its own author clearly did not intend. Bounded rather than 100 tries
because each attempt is a Telegram API call — a group whose early history is
entirely deleted would otherwise spend a hundred round trips discovering that,
per command, for every member who types it.

Exhausting every attempt still answers nothing at all, exactly as v1 does when
its single attempt misses.
"""

from __future__ import annotations

import random
from typing import cast

from aiogram import Bot, Router
from aiogram.types import Message

from cb_core.logging import get_logger
from cb_gateway.context import context_for, deny_if_disabled
from cb_gateway.filters import CommandName

log = get_logger("cb.gateway.unearth")

router = Router(name="unearth")

#: How many message ids to try before giving up. Each one is a `forwardMessage`
#: round trip, so this trades "answers more often" against "costs the API more
#: on a group whose old messages are gone".
_ATTEMPTS = 8


def pick_id(newest_message_id: int, rng: random.Random | None = None) -> int:
    """A candidate id in v1's own range: `random.randint(1, msg['message_id'])`.

    Inclusive of the command's own id, as v1's is — unearthing the command
    itself is a legal, if unexciting, outcome, and excluding it would be a
    change to the distribution rather than a fix to anything.
    """
    chooser = rng or random
    return chooser.randint(1, max(newest_message_id, 1))


@router.message(CommandName("unearth"))
async def unearth(message: Message) -> None:
    bot = cast(Bot, message.bot)
    ctx = await context_for(bot, message)
    if await deny_if_disabled(message, ctx, "fun"):
        return

    for _ in range(_ATTEMPTS):
        candidate = pick_id(message.message_id)
        try:
            await bot.forward_message(
                chat_id=message.chat.id,
                from_chat_id=message.chat.id,
                message_id=candidate,
                # v1 threads the topic through (`thread_id=thread_id`), so an
                # unearthed message lands in the topic it was asked for rather
                # than in the group's General.
                message_thread_id=message.message_thread_id,
            )
            return
        except Exception as exc:  # noqa: BLE001 - a miss is the normal case, not a failure
            log.debug("unearth.miss", candidate=candidate, error=str(exc))

    # Silence, as v1 is silent when its one attempt misses. Logged at info
    # because "the command never answers in this group" is a real support
    # question, and this is the line that answers it.
    log.info("unearth.exhausted", attempts=_ATTEMPTS, newest=message.message_id)


__all__ = ["pick_id", "router", "unearth"]
