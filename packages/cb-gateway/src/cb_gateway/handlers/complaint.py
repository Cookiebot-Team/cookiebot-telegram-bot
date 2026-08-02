"""fun_complaint — `/complaint` (Milton from HR) and its reply-triggered hold.

v1: `complaint`/`complaint_answer`, `../COOKIEBOT-Telegram-Group-Bot/Bot/Miscellaneous.py:240-259`,
dispatched `COOKIEBOT.py:215,234-235` (entry 1) and `:300-301` (entry 2). See
`.specs/features/fun_complaint/spec.md` and `design.md` for the full behaviour
contract; `docs/contracts/fun_complaint.md` carries the same once T6 lands.

Two stateless entry points, no database (spec: "Persistence: none"):

- Entry 1, `complaint` (`Miscellaneous.py:240-248`): sends a photo of Milton
  from HR, captioned with an invitation to reply with a complaint.
- Entry 2, `complaint_answer` (`Miscellaneous.py:250-259`): fires when someone
  replies to that photo. Deletes the photo, puts the user "on hold" with a
  voice note of hold music stamped with a fake protocol number, then — after
  10-20 seconds — deletes the hold note and answers with a random canned line.

D-CP-4 (spec.md): v1 blocks a worker thread for the whole hold with
`time.sleep(random.randint(10, 20))` (`Miscellaneous.py:256`). AGENTS.md §2.4
forbids blocking the reply path, so the hold's tail is scheduled with
`asyncio.create_task` instead — see `_schedule_tail` below, modelled verbatim
on `groupguardian.py:501-517`'s captcha-unban idiom.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
from datetime import UTC, datetime
from typing import Final, cast

from aiogram import Bot, Router
from aiogram.types import FSInputFile, Message

from cb_core import assets, locales
from cb_core.textmatch import ParsedCommand
from cb_gateway.context import context_for, t
from cb_gateway.filters import CommandName
from cb_gateway.telemetry import mark_outcome

router = Router(name="complaint")

# The English and Portuguese `complaint` catalog strings
# (`cb_core/locale_data/{en,pt}/lib.json`, key `"complaint"`) end in these two
# literal substrings, and entry 2 (`_is_milton_reply`) matches a replied-to
# photo's caption against them (design R2.5, D-CP-3: substring containment,
# not equality). Editing either locale value breaks the reply chain — a photo
# sent under the old caption would no longer re-arm entry 2, and vice versa.
MILTON_SIGNATURES: Final[tuple[str, str]] = ("Milton do RH.", "Milton from HR.")

# v1: `random.randint(10, 20)` (`Miscellaneous.py:256`) — the hold's duration
# before the tail fires, in seconds.
_MIN_HOLD_SECONDS: Final = 10
_MAX_HOLD_SECONDS: Final = 20

# One shared rng for every random draw in this module (protocol digits, hold
# file, delay, answer line) — plain `random`, not `secrets`, matching v1's own
# `random.randint`/`random.choice` (same note as `fun_ship`/`fun_dice`).
# `_build_protocol` takes its own `random.Random` argument so a seeded rng can
# be handed to it directly in tests without touching this instance.
_rng = random.Random()

# A bare `asyncio.create_task(...)` is only weakly referenced by the event
# loop and can be garbage-collected mid-sleep, silently dropping the deletion
# and the answer. Held here until each task finishes — the exact idiom
# `groupguardian.py:498` uses for the captcha's 30s unban.
_pending_tails: set[asyncio.Task[None]] = set()

# The tail's `asyncio.sleep` as a module attribute so a test can monkeypatch it
# to a no-op instead of actually waiting 10-20s (design R3.4). Never
# `time.sleep` — that would reintroduce D-CP-4 on the async loop.
_sleep = asyncio.sleep


# --------------------------------------------------------------------- pure helpers


def _photo_filename(lang: str) -> str:
    """D-CP-2: `"pt" if lang == "pt" else "eng"` (`Miscellaneous.py:243-247`).

    An equality check against the resolved language, not a locale lookup —
    there is no `milton_es.jpg`, so every non-`pt` language, Spanish included,
    falls through to the English photo. Preserved, not fixed (spec verdict).
    """
    return "milton_pt.jpg" if lang == "pt" else "milton_eng.jpg"


def _build_protocol(rng: random.Random) -> str:
    """v1: `f"{randint(10,99)}-{randint(100000,999999)}/{now().year}"`
    (`Miscellaneous.py:253`). Generated, shown, and never stored (spec:
    Persistence — none)."""
    year = datetime.now(UTC).year
    return f"{rng.randint(10, 99)}-{rng.randint(100000, 999999)}/{year}"


def _is_milton_reply(message: Message) -> bool:
    """Structural precondition for entry 2, modelled on
    `rules.py:_is_new_rules_reply` / `welcome.py:_is_welcome_reply`.

    v1's whole command-dispatch chain lives inside `if text.startswith("/") and
    len(text) > 1`, and the reply-capture branch is a sibling `elif` of that
    `if` (`COOKIEBOT.py:186,300-301`) — reachable only when the incoming
    message has text and does not itself look like a command. The one
    structural difference from `_is_new_rules_reply`: this reads the replied-to
    message's *photo caption*, not its `.text` (D-CP-3), with substring
    containment over both `MILTON_SIGNATURES` rather than equality.
    """
    text = message.text
    if text is None:
        return False
    if text.startswith("/") and len(text) > 1:
        return False
    reply = message.reply_to_message
    if reply is None:
        return False
    caption = reply.caption
    if caption is None:
        return False
    return any(signature in caption for signature in MILTON_SIGNATURES)


# ------------------------------------------------------------------- entry 1


@router.message(CommandName("complaint"))
async def complaint(message: Message, parsed: ParsedCommand | None = None) -> None:
    """`/milton`, `/reclamacao`, `/reclamação`, `/complaint`, `/queja` — all
    five already map to the `complaint` canonical name in
    `cb_core/textmatch.py:COMMAND_ALIASES`. v1: `Miscellaneous.py:240-248`."""
    if parsed is None:  # pragma: no cover - CommandName always injects a match
        return

    bot = cast(Bot, message.bot)
    ctx = await context_for(bot, message)
    if not ctx.enabled("fun"):
        # v1: `notify_fun_off` (`Miscellaneous.py:129-131`), a reply, not a
        # bare send — same idiom as `ship.py`.
        mark_outcome("refused")
        await message.reply(t(ctx, "fun_off"))
        return

    # v1 has no try/except here (spec: "Failure output: none... everything
    # else propagates to the dispatcher's bare except") — only the deletions
    # in entry 2 are swallowed. A failed chat-action or photo send is left to
    # propagate, matching that.
    await bot.send_chat_action(message.chat.id, "upload_photo")

    sender = message.from_user
    first_name = sender.first_name if sender is not None else ""
    caption = t(ctx, "complaint", user=first_name)
    photo = FSInputFile(assets.path("complaint", _photo_filename(ctx.lang)))
    await message.reply_photo(photo, caption=caption)


# ------------------------------------------------------------------- entry 2


@router.message(_is_milton_reply)
async def complaint_answer(message: Message) -> None:
    """v1: `Miscellaneous.py:250-259`. Deletes the Milton photo, sends the
    hold-music voice note, then schedules the delayed tail (see
    `_schedule_tail`) instead of blocking on it."""
    bot = cast(Bot, message.bot)
    ctx = await context_for(bot, message)
    if not ctx.enabled("fun"):
        mark_outcome("refused")
        await message.reply(t(ctx, "fun_off"))
        return

    reply = message.reply_to_message
    assert reply is not None  # guaranteed by _is_milton_reply
    # v1's `delete_message` swallows its own errors
    # (`universal_funcs.py:340-344`) — the photo may already be gone.
    with contextlib.suppress(Exception):
        await reply.delete()

    await bot.send_chat_action(message.chat.id, "upload_audio")
    protocol = _build_protocol(_rng)
    hold_file = _rng.choice(assets.pool("complaint", suffix=".wav"))
    voice_message = await message.reply_voice(
        FSInputFile(hold_file), caption=f"Protocol: {protocol}"
    )

    _schedule_tail(bot, message, voice_message.message_id, ctx.lang)


def _schedule_tail(
    bot: Bot,
    message: Message,
    voice_message_id: int,
    lang: str,
    *,
    delay: int | None = None,
) -> None:
    """Schedules the delayed deletion + answer without blocking the reply path
    (D-CP-4). `delay` is accepted explicitly so a test can pass `0` directly;
    absent that, it is drawn from `_rng` the same way v1 drew it inline.

    Restart caveat: still an in-process `asyncio.create_task`, exactly the
    `groupguardian.py:501-517` idiom for the captcha's 30s unban — a gateway
    restart inside the hold window loses the scheduled deletion and answer,
    because gateway->worker enqueue wiring does not exist yet (`HANDOFF.md`
    §1, known gap 5). Not fixed in this port (design R3.3); revisit once
    `util_everyone` (or whichever feature lands the wiring first) builds it.
    """
    resolved_delay = _rng.randint(_MIN_HOLD_SECONDS, _MAX_HOLD_SECONDS) if delay is None else delay
    task = asyncio.create_task(
        _delayed_reveal(bot, message, voice_message_id, lang, resolved_delay)
    )
    _pending_tails.add(task)
    task.add_done_callback(_pending_tails.discard)


async def _delayed_reveal(
    bot: Bot, message: Message, voice_message_id: int, lang: str, delay: int
) -> None:
    """v1: `sleep(randint(10,20))` -> delete the voice note -> a random answer
    line (`Miscellaneous.py:256-259`), off the reply path per D-CP-4."""
    await _sleep(delay)
    with contextlib.suppress(Exception):
        await bot.delete_message(message.chat.id, voice_message_id)
    await message.reply(_rng.choice(locales.lines("answers", lang)))


__all__ = [
    "MILTON_SIGNATURES",
    "complaint",
    "complaint_answer",
    "router",
]
