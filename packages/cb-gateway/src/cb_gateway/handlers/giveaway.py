"""x_giveaways — `/giveaway <prize>` and the four button presses behind it.

v1: `../COOKIEBOT-Telegram-Group-Bot/Bot/Giveaways.py:25-173`, dispatched
`COOKIEBOT.py:249,262-263` under the `functionsUtility` gate (the second
`elif` chain, not the `funfunctions` one) and `COOKIEBOT.py:415-428` for the
callbacks. Contract: `docs/contracts/x_giveaways.md`. No QA scenario exists
anywhere for this feature — `qa/features/x_giveaways.feature` is authored, not
ported (AGENTS.md §5's "20+ v1 features have no QA scenario; write it as part
of the port").

The flow is v1's, unchanged: a prize, a keyboard offering one to five winners,
a pinned announcement with an "enter" and an "end" button, a draw that posts
each winner with their profile photo, and a "draw more winners?" follow-up
that either draws again or closes the raffle. Callback data keeps v1's exact
wire format (`GIVEAWAY <n>` / `GIVEAWAY enter` / `GIVEAWAY end` /
`GIVEAWAY delete`) so nothing about a press changes.

Four things are deliberately **not** reproduced, each recorded in the contract:

* **D-GA-1 — v1's `/giveaway` never completed.** `giveaways_ask` put
  `json.dumps(prize_text)[:20]` in the callback data (`:36`), the dispatcher
  stripped every `"` back out of it (`COOKIEBOT.py:421`), and
  `giveaways_create` then called `json.loads` on the result (`:54`) — which
  raises for any prize that is not a bare JSON literal, i.e. every real one.
  The exception went to v1's top-level `except`, so the user saw nothing. The
  prize is carried in `cb_core.pending_giveaways` here instead, untruncated
  and unmangled; see that module for the full reasoning.
* **D-GA-2 — the "Put me in!" button was admin-only.** v1 gates *every*
  `GIVEAWAY` callback on the admin check (`COOKIEBOT.py:416-418`), including
  `enter`, so no ordinary member could ever join the raffle the button invites
  them to. That it is a defect rather than a policy is visible in v1's own
  code: `giveaways_end` re-checks admin/creator/owner itself (`:114-117`) and
  answers `giveaway.end_adm`, which would be dead code if the outer gate were
  intended to cover it. Here `enter` is open to everyone and the outer gate
  survives on the two branches that genuinely change the raffle (creating it,
  deleting it).
* **The `telegram.me` profile-photo scrape.** v1's `get_profile_image`
  (`SocialContent.py:279-292`) fetches a user's public web preview with
  BeautifulSoup and re-encodes it through OpenCV into a fixed `temp.jpg`
  (`:144-146`) — a cross-request race on a shared filename, and the same
  mechanism `fun_battle`'s port already replaced. This handler holds the
  entrant's real `user_id`, so it asks the Bot API directly
  (`get_user_profile_photos`) and forwards the `file_id`; no download, no
  re-encode, no temp file.
* **The lost-update on entry, and name-based identity.** See
  `cb_core/giveaways.py` and migration `0006`.
"""

from __future__ import annotations

import contextlib
import random
from dataclasses import dataclass
from typing import cast

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from whenever import Instant

from cb_core import giveaways, locales, pending_giveaways
from cb_core.giveaways import Giveaway, Participant
from cb_core.logging import get_logger
from cb_core.settings import get_settings
from cb_core.textmatch import ParsedCommand
from cb_gateway.context import ChatContext, context_for, t
from cb_gateway.filters import CommandName
from cb_gateway.telemetry import mark_outcome

log = get_logger("cb.giveaway")

router = Router(name="giveaway")

#: v1's callback prefix, byte for byte (`Giveaways.py:41-45,61-62`).
CALLBACK_PREFIX = "GIVEAWAY"

#: v1 offers exactly these five counts (`:41-45`) and `giveaways_create`
#: rejects anything outside them a second time (`:49`).
WINNER_CHOICES: tuple[int, ...] = (1, 2, 3, 4, 5)

#: v1's hardcoded English refusal for an unauthorised `GIVEAWAY` press
#: (`COOKIEBOT.py:417`) — not a catalog key, so it is not translated here
#: either.
NOT_ADMIN_TEXT = "Only admins can do this"


# --------------------------------------------------------------------- strings


#: v1 keeps this feature's strings in a nested `giveaway` object rather than at
#: the top level, so every lookup goes through `locales.get_nested` — which
#: falls back to `en` **per entry**, the behaviour `es` needs here: its
#: `giveaway` object exists but is missing ten of its sixteen entries (v1's own
#: catalog drift, reported by `locales.missing_keys()`).
_SECTION = "giveaway"


def gtext(lang: str, key: str, **fmt: object) -> str:
    """One `giveaway.*` string in `lang`, falling back to `en` then the key."""
    return locales.get_nested(_SECTION, key, lang, **fmt)


def button_labels(lang: str) -> tuple[str, str]:
    """`giveaway.buttons` — a two-element JSON array, not a string (`:58`)."""
    value = locales.nested_value(_SECTION, "buttons", lang)
    if not isinstance(value, list) or len(value) < 2:
        value = locales.nested_value(_SECTION, "buttons", "en")
    labels = cast(list[str], value)
    return labels[0], labels[1]


# ------------------------------------------------------------------- callbacks


@dataclass(frozen=True, slots=True)
class GiveawayPress:
    """A parsed `GIVEAWAY ...` press.

    `action` is v1's own vocabulary (`COOKIEBOT.py:419-428`): the literal
    `"1".."5"`, `"enter"`, `"end"` or `"delete"`. An unknown action is
    returned rather than rejected, so the handler can answer v1's
    "ERROR! please contact @MekhyW" branch instead of ignoring the press.

    `token` is the trailing field. v1 put the (mangled) prize there; here it
    is the `cb_core.pending_giveaways` handle, and it is empty for the three
    actions that address an already-created raffle.
    """

    action: str
    token: str = ""


def parse_callback_data(data: str) -> GiveawayPress | None:
    """A `GIVEAWAY ...` press, or `None` for anything else."""
    parts = data.split()
    if len(parts) < 2 or parts[0] != CALLBACK_PREFIX:
        return None
    return GiveawayPress(action=parts[1], token=parts[2] if len(parts) > 2 else "")


def _is_giveaway_callback(callback: CallbackQuery) -> bool:
    return parse_callback_data(callback.data or "") is not None


def winner_keyboard(token: str) -> InlineKeyboardMarkup:
    """v1's one-button-per-row count picker (`:40-46`). The labels are the
    digits themselves, so this one carries no language; `token` is what v1
    carried the prize itself in."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=str(n), callback_data=f"{CALLBACK_PREFIX} {n} {token}")]
            for n in WINNER_CHOICES
        ]
    )


def entry_keyboard(lang: str) -> InlineKeyboardMarkup:
    """v1's live-giveaway keyboard: enter, and end (`:60-63`)."""
    enter_label, end_label = button_labels(lang)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=enter_label, callback_data=f"{CALLBACK_PREFIX} enter")],
            [InlineKeyboardButton(text=end_label, callback_data=f"{CALLBACK_PREFIX} end")],
        ]
    )


def draw_more_keyboard() -> InlineKeyboardMarkup:
    """v1's post-draw keyboard — the two emoji are the labels (`:150-153`)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅", callback_data=f"{CALLBACK_PREFIX} end")],
            [InlineKeyboardButton(text="❌", callback_data=f"{CALLBACK_PREFIX} delete")],
        ]
    )


# ----------------------------------------------------------------- pure helpers


def announcement_text(lang: str, prize: str, winners: int) -> str:
    """v1's `giveaway.time` with its three substitutions (`:52-57`).

    The date is formatted with the catalog's own per-language `strftime`
    pattern (`%m/%d` for `en`, `%d/%m` for `pt`/`es`) against the host's local
    time, exactly as v1's `datetime.datetime.now()` did.
    """
    stamp = Instant.now().to_system_tz().to_stdlib().strftime(gtext(lang, "strftime"))
    return gtext(lang, "time", prize=prize, win=winners, date=stamp)


def display_name_for(user_id: int, username: str | None, first_name: str | None) -> str:
    """v1's entrant label: `"@" + username`, else the first name (`:77`)."""
    if username:
        return f"@{username}"
    return first_name or str(user_id)


def pick_winners(
    entrants: tuple[Participant, ...],
    winners_wanted: int,
    rng: random.Random | None = None,
) -> list[Participant]:
    """v1's `random.sample(participants, min(n_winners, len(participants)))`
    (`:128-129`). `rng` follows `firecracker.py`/`battle.py`'s convention:
    `None` in production, a seeded `random.Random` in tests."""
    take = min(winners_wanted, len(entrants))
    sampler = rng.sample if rng is not None else random.sample
    return list(sampler(list(entrants), take)) if take else []


def winner_caption(lang: str, *, index: int, winner: str, prize: str, total: int) -> str:
    """v1 picks the singular key by the *configured* winner count, not by how
    many were actually drawn (`:131`) — preserved: a 3-winner raffle with one
    entrant still reads "our 1st winner is…"."""
    key = "one" if total == 1 else "more"
    # `"winnner"` is v1's own typo in every locale file (`:131`); the catalog
    # is a byte-for-byte port, so the typo is the key. Two levels deep, which
    # `get_nested` does not model — it resolves one object, and this is an
    # object inside it.
    nested = locales.nested_value(_SECTION, "winnner", lang)
    if not isinstance(nested, dict) or key not in nested:
        nested = locales.nested_value(_SECTION, "winnner", "en")
    template = cast(dict[str, object], nested or {}).get(key)
    if not isinstance(template, str):
        return f"giveaway.winnner.{key}"
    try:
        return template % {"idx": index, "winner": winner, "prize": prize}
    except (KeyError, ValueError, TypeError):
        return template


def _is_owner(ctx: ChatContext) -> bool:
    owner_id = get_settings().owner_id
    return bool(owner_id) and ctx.actor.user_id == owner_id


def may_manage(ctx: ChatContext) -> bool:
    """v1's outer `GIVEAWAY` gate (`COOKIEBOT.py:416-418`) and the same check
    `giveaways_ask` makes before creating one (`:27`)."""
    return ctx.is_admin or _is_owner(ctx)


def may_end(ctx: ChatContext, giveaway: Giveaway) -> bool:
    """v1's inner check in `giveaways_end` (`:114-117`): an admin, the person
    who created the raffle, or the bot owner."""
    return may_manage(ctx) or ctx.actor.user_id == giveaway.creator_id


# --------------------------------------------------------------------- handlers


@router.message(F.chat.type != ChatType.PRIVATE, CommandName("giveaway"))
async def giveaway_ask(message: Message, parsed: ParsedCommand | None = None) -> None:
    """`/giveaway <prize>`. v1: `giveaways_ask` (`:25-46`)."""
    if parsed is None:  # pragma: no cover - CommandName always injects a match
        return

    ctx = await context_for(cast(Bot, message.bot), message)
    if not ctx.enabled("utility"):
        # v1: notify_utility_off (COOKIEBOT.py:252), the same reply-shaped
        # refusal `util_youtube` makes from the same dispatch block.
        mark_outcome("refused")
        await message.reply(t(ctx, "utility_off"))
        return

    if not may_manage(ctx):
        mark_outcome("refused")
        await message.reply(gtext(ctx.lang, "permission"))
        return

    prize = parsed.args.strip()
    if not prize:
        # v1: `len(msg['text'].split()) == 1` (`:31-34`).
        await message.reply(gtext(ctx.lang, "raffled"))
        return

    token = pending_giveaways.new_token()
    await pending_giveaways.put(token, prize)
    await message.reply(gtext(ctx.lang, "create"), reply_markup=winner_keyboard(token))


@router.callback_query(_is_giveaway_callback)
async def press_giveaway_button(callback: CallbackQuery, bot: Bot) -> None:
    """Every `GIVEAWAY` press. v1: `COOKIEBOT.py:415-428`.

    v1 answers each branch's callback itself; the two branches it forgets
    (the numeric one, and the unauthorised early return's sibling paths) leave
    a client spinning, so every path here answers exactly once — the same rule
    `util_config`'s port already applies.
    """
    press = parse_callback_data(callback.data or "")
    if press is None or callback.message is None:  # pragma: no cover - filter checked
        await callback.answer()
        return

    ctx = await context_for(bot, callback)
    message_id = callback.message.message_id

    if press.action.isdigit():
        await _create(callback, bot, ctx, message_id, int(press.action), press.token)
        return
    if press.action == "enter":
        await _enter(callback, ctx, message_id)
        return
    if press.action == "end":
        await _end(callback, bot, ctx, message_id)
        return
    if press.action == "delete":
        await _delete(callback, bot, ctx, message_id)
        return
    # v1's trailing else (`COOKIEBOT.py:428`), verbatim.
    await callback.answer(text="ERROR! please contact @MekhyW")


async def _create(
    callback: CallbackQuery,
    bot: Bot,
    ctx: ChatContext,
    prompt_message_id: int,
    winners: int,
    token: str,
) -> None:
    """The count press. v1: `giveaways_create` (`:48-72`)."""
    if not may_manage(ctx):
        mark_outcome("refused")
        await callback.answer(text=NOT_ADMIN_TEXT)
        return
    if winners not in WINNER_CHOICES:
        # v1 returns silently here (`:49-50`), which leaves the client
        # spinning; answering is the same fix as everywhere else in this file.
        await callback.answer()
        return

    prize = await pending_giveaways.take(token) if token else None
    if prize is None:
        await callback.answer(text=gtext(ctx.lang, "not_found"))
        return

    # v1 deletes the prompt before creating (`COOKIEBOT.py:420`).
    with contextlib.suppress(Exception):
        await bot.delete_message(ctx.group_id, prompt_message_id)

    announcement = await bot.send_message(
        ctx.group_id,
        announcement_text(ctx.lang, prize, winners),
        reply_markup=entry_keyboard(ctx.lang),
    )
    await giveaways.create(
        group_id=ctx.group_id,
        message_id=announcement.message_id,
        creator_id=callback.from_user.id,
        prize=prize,
        winners_wanted=winners,
    )
    # v1 pins it and swallows the failure (`:69-72`) — a bot without pin
    # rights still runs a giveaway.
    with contextlib.suppress(Exception):
        await bot.pin_chat_message(ctx.group_id, announcement.message_id)
    await callback.answer()


async def _enter(callback: CallbackQuery, ctx: ChatContext, message_id: int) -> None:
    """The "Put me in!" press — open to every member, see D-GA-2 in the module
    docstring. v1: `giveaways_enter` (`:74-99`)."""
    giveaway = await giveaways.by_message(ctx.group_id, message_id)
    if giveaway is None:
        await callback.answer(text=gtext(ctx.lang, "not_found"))
        return

    user = callback.from_user
    try:
        joined = await giveaways.enter(
            ctx.group_id,
            giveaway.giveaway_id,
            user_id=user.id,
            display_name=display_name_for(user.id, user.username, user.first_name),
        )
    except Exception as exc:  # noqa: BLE001 - v1's own `except` around the whole body (`:97`)
        log.warning("giveaway.enter_failed", error=str(exc))
        await callback.answer(text=gtext(ctx.lang, "error"))
        return

    await callback.answer(text=gtext(ctx.lang, "enter" if joined else "in"))


async def _end(callback: CallbackQuery, bot: Bot, ctx: ChatContext, message_id: int) -> None:
    """The draw. v1: `giveaways_end` (`:101-163`)."""
    giveaway = await giveaways.by_message(ctx.group_id, message_id)
    if giveaway is None:
        await callback.answer(text=gtext(ctx.lang, "not_found"))
        return
    if not may_end(ctx, giveaway):
        mark_outcome("refused")
        await callback.answer(text=gtext(ctx.lang, "end_adm"))
        return

    entrants = await giveaways.participants(ctx.group_id, giveaway.giveaway_id)
    if not entrants:
        # v1 announces it in the group, deletes the raffle and removes the
        # message (`:118-126`).
        await bot.send_message(ctx.group_id, gtext(ctx.lang, "no_one"))
        await giveaways.delete(ctx.group_id, giveaway.giveaway_id)
        await callback.answer(text=gtext(ctx.lang, "end"))
        with contextlib.suppress(Exception):
            await bot.delete_message(ctx.group_id, message_id)
        return

    for index, winner in enumerate(pick_winners(entrants, giveaway.winners_wanted), start=1):
        caption = winner_caption(
            ctx.lang,
            index=index,
            winner=winner.display_name,
            prize=giveaway.prize,
            total=giveaway.winners_wanted,
        )
        await _announce_winner(bot, ctx.group_id, winner, caption)

    # v1 posts the follow-up and re-points the row at it (`:149-157`). The
    # entrants deliberately survive, so "draw more" draws from the same pool —
    # a previous winner can be drawn again, which is what v1 does.
    follow_up = await bot.send_message(
        ctx.group_id, gtext(ctx.lang, "draw_more"), reply_markup=draw_more_keyboard()
    )
    await giveaways.repoint(ctx.group_id, giveaway.giveaway_id, follow_up.message_id)
    await callback.answer(text=gtext(ctx.lang, "selected"))
    with contextlib.suppress(Exception):
        await bot.delete_message(ctx.group_id, message_id)


async def _announce_winner(bot: Bot, group_id: int, winner: Participant, caption: str) -> None:
    """One winner, with their profile photo when Telegram will give us one.

    v1 scraped `telegram.me` and fell back to a plain message when the scrape
    produced nothing (`:138-148`); the fallback is preserved, the scrape is
    not — see the module docstring.
    """
    file_id: str | None = None
    try:
        photos = await bot.get_user_profile_photos(winner.user_id, limit=1)
        if photos.photos:
            file_id = photos.photos[0][-1].file_id
    except Exception as exc:  # noqa: BLE001 - a missing photo is v1's own fallback, not an error
        log.info("giveaway.photo_unavailable", error=str(exc))

    if file_id is None:
        await bot.send_message(group_id, caption)
        return
    await bot.send_photo(group_id, file_id, caption=caption)


async def _delete(callback: CallbackQuery, bot: Bot, ctx: ChatContext, message_id: int) -> None:
    """The ❌ press. v1: `giveaways_delete` (`:165-173`), which has no check of
    its own and relies entirely on the outer gate — so that gate is kept here."""
    if not may_manage(ctx):
        mark_outcome("refused")
        await callback.answer(text=NOT_ADMIN_TEXT)
        return
    giveaway = await giveaways.by_message(ctx.group_id, message_id)
    if giveaway is not None:
        await giveaways.delete(ctx.group_id, giveaway.giveaway_id)
    await callback.answer(text=gtext(ctx.lang, "end"))
    with contextlib.suppress(Exception):
        await bot.delete_message(ctx.group_id, message_id)


__all__ = [
    "CALLBACK_PREFIX",
    "NOT_ADMIN_TEXT",
    "WINNER_CHOICES",
    "GiveawayPress",
    "announcement_text",
    "button_labels",
    "display_name_for",
    "draw_more_keyboard",
    "entry_keyboard",
    "giveaway_ask",
    "gtext",
    "may_end",
    "may_manage",
    "parse_callback_data",
    "pick_winners",
    "press_giveaway_button",
    "router",
    "winner_caption",
    "winner_keyboard",
]
