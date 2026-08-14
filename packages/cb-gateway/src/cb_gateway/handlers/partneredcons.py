"""fun_partneredcons — `/bff`, `/patas`, `/fursmeet`, `/furcamp`, `/pawstral`
and `/trex`: a poster for a partnered convention, captioned with how many days
are left until it.

v1: `event_countdown`,
`../COOKIEBOT-Telegram-Group-Bot/Bot/Miscellaneous.py:261-323`, dispatched
`COOKIEBOT.py:248-251`. Full contract: `docs/contracts/fun_partneredcons.md`.
Spec/design: `.specs/features/fun_partneredcons/`.

## Ungated, unlike every other command in the same chain

The dispatch `elif` for these five names sits *above* the
`elif not utilityfunctions: notify_utility_off(...)` branch (`COOKIEBOT.py:253`)
and outside the `functionsFun` block entirely, so a group with both feature
switches off still gets its convention posters. Preserved: it is what v1 does,
and these are promotional posts for real partners rather than a fun toy.

## The caption is Python, not the locale catalog

`cb_core/locale_data/*/lib.json`'s `event` key carries `name`, a `cta` list and
a `caption` template per event, and **v1 reads only `cta`** (`:266` etc.) and
`event.error` (`:320`). Every real caption is an f-string in v1's source, in
Portuguese for four of the five events and English for `pawstral`, regardless
of the group's language. That mismatch is user-visible — a Spanish-speaking
group gets a Portuguese countdown — and AGENTS.md's tie-break (executing code
beats inert data) makes it the thing to preserve. The `caption` templates in
the catalog stay unread here, exactly as in v1.

## The `+365` wraparound

Each event's date is a hardcoded `(day, month, year)` — the next occurrence as
of whenever someone last edited that line, not a recurring rule. Once it
passes, `while daysremaining < -5: daysremaining += 365` walks the count
forward in 365-day hops (never 366; leap years are not accounted for) until it
is no longer more than five days in the past, while the caption keeps printing
the *original* hardcoded day and month. So the number is "days until the same
calendar date, some number of 365-day hops away" and drifts a day every four
years. Preserved verbatim: this is a content-maintenance quirk in v1's
hardcoded dates, and "fixing" it would mean inventing real convention dates.

Between five days before the date and the date itself
(`-5 <= daysremaining <= 0`), the caption is replaced by a bare YouTube link —
the same link for every event, v1's placeholder for "it is happening right
now" (`:270`).

## `/trex` is net-new, and deliberately minimal

`/trex` appears in `../Cookiebot-QA/features/fun_partneredcons.feature` and
nowhere in v1's source — not even as dead code. What it *does* have is
`Countdown/Trex`, 67 images sitting in v1's bucket that no v1 code path ever
listed (`bucket_export.PREFIXES` records how they were found). This port sends
one of them and stops: no countdown, because there is no date for the event
anywhere in any of the three reference repos and inventing one would be
fabricating content for a real-world event; no caption, because every caption
string this feature has is an f-string about a specific date. QA asks for "a
picture of the Trex Furplayer event", which is exactly what it gets. If a date
and a caption are supplied later, `_EVENTS` is where they go and nothing else
changes.
"""

from __future__ import annotations

import datetime as dt
import random
from posixpath import splitext
from typing import cast

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.types import BufferedInputFile, Message, ReactionTypeEmoji
from msgspec import Struct

from cb_core import legacy_assets, locales, storage
from cb_core.logging import get_logger
from cb_core.publisher import number_to_emojis
from cb_core.textmatch import ParsedCommand
from cb_gateway.context import context_for
from cb_gateway.filters import CommandName

log = get_logger("cb.gateway.partneredcons")

router = Router(name="partneredcons")

#: v1's "the event is happening right now" caption — one link, every event
#: (`Miscellaneous.py:270,281,292,303,314`).
_HAPPENING_NOW = "https://www.youtube.com/watch?v=JsOVJ1PAC6s&ab_channel=TheVibeGuide"


class Event(Struct, frozen=True):
    """One convention: v1's hardcoded date, its bucket prefix, and the
    caption template its branch builds.

    `caption` is a `str.format` template rather than an f-string so the whole
    table can be data; the fields it may use are `days` (already keycapped),
    `cta`, `day`, `month`, `year` and `day_end` — the last being v1's own
    `day + N`, where N differs per event (3 for patas, 2 for bff, 4 for
    furcamp…) and is baked into each template's arithmetic at build time.
    """

    command: str  # the canonical name in `textmatch.COMMAND_ALIASES`
    prefix: str  # the `legacy_assets` catalog key
    cta_key: str = ""  # `event.<cta_key>.cta`; empty for /trex
    date: tuple[int, int, int] = ()  # type: ignore[assignment]  # (day, month, year); empty for /trex
    caption: str = ""  # `str.format` template; empty for /trex
    days_span: int = 0  # v1's `day + N` in the "when" line


# fmt: off
#: The five ported events plus `/trex`, in v1's own `elif` order
#: (`Miscellaneous.py:264-318`). Dates, venue names, ticket links and group
#: handles are copied verbatim from those f-strings — including the ones that
#: are already in the past (`fursmeet` 2025, `pawstral` 2025, `bff` 2026),
#: because the wraparound above is what v1 does with them.
_EVENTS: tuple[Event, ...] = (
    Event(
        "con_patas", "Countdown/Patas", "patas", (11, 12, 2026), days_span=3,
        caption=(
            "<b> Faltam {days} dias para o Patas! </b>\n\n<i> {cta} </i>\n"
            "🐾🍌🐾🐒🐾🍌🐾🐒🐾🍌🐾🐒🐾🍌\n\n"
            "📆 {day} a {day_end}/{month}, Sorocaba Park Hotel\n"
            "💻 Ingressos em: patas.site\n"
            "📲 Grupo do evento: @EventoPatas"
        ),
    ),
    Event(
        "con_bff", "Countdown/BFF", "bff", (17, 7, 2026), days_span=2,
        caption=(
            "<b> Faltam {days} dias para a Brasil FurFest 2026 - Sem Tempo Irmão! </b>\n\n"
            "<i> {cta} </i>\n🐾🟩🐾🟨🐾🟩🐾🟨🐾🟩🐾🟨🐾🟩\n\n"
            "📆 {day} a {day_end}/{month}\n📍 Hotel Premium - Campinas\n"
            "💻 Site: brasilfurfest.com.br, upgrades até 1 mês antes do evento "
            "através do email reg@brasilfurfest.com.br\n"
            "📲 Grupo do evento: @brasilfurfest"
        ),
    ),
    Event(
        "con_fursmeet", "Countdown/FurSMeet", "fursmeet", (21, 11, 2025), days_span=2,
        caption=(
            "<b> Faltam {days} dias para o FurSMeet {year}! </b>\n\n<i> {cta} </i>\n"
            "🦕🦖🦫🦕🦖🦫🦕🦖🦫🦕🦖🦫🦕🦖🦫\n\n"
            "📆 {day} a {day_end}/{month}, Santa Maria, Rio Grande do Sul\n"
            "🎫Link para comprar ingresso: fursmeet.carrd.co\n"
            "💻 Informações no site: fursmeet.wixsite.com/fursmeet\n"
            "📲 Grupo do evento: @fursmeetchat"
        ),
    ),
    Event(
        "con_furcamp", "Countdown/Furcamp", "furcamp", (5, 2, 2027), days_span=4,
        caption=(
            "<b> Faltam {days} dias para o FurCamp! </b>\n\n<i> {cta} </i>\n"
            "🐾🌲🐾🌳🐾🌲🐾🌳🐾🌲🐾🌳\n\n"
            "📆 {day} a {day_end}/{month}\n📍 Acampamento Aruanã, Embu-Guaçu - SP\n"
            "💻 Ingressos em: furcamp.com\n"
            "📲 Grupo do evento: @FurcampOficial"
        ),
    ),
    Event(
        "con_pawstral", "Countdown/Pawstral", "pawstral", (29, 8, 2025), days_span=2,
        caption=(
            "<b> {days} days left until Pawstral! </b>\n\n<i> {cta} </i>\n"
            "🇨🇱⭐🐈🇨🇱⭐🐈🇨🇱⭐🐈🇨🇱⭐🐈🇨🇱⭐🐈\n\n"
            "📆 {day} a {day_end}/{month}, Santiago de Chile\n"
            "💻 Tickets at: https://pawstral.cl/\n"
            "📲 Event chat: @PawstralFurcon"
        ),
    ),
    # Net-new; no date, no caption, no cta — see the module docstring.
    Event("con_trex", "Countdown/Trex"),
)
# fmt: on

_BY_COMMAND: dict[str, Event] = {event.command: event for event in _EVENTS}

_rng = random.Random()


# --------------------------------------------------------------------- pure logic


def days_remaining(date: tuple[int, int, int], now: dt.datetime) -> int:
    """v1: `(datetime(year, month, day) - datetime.now()).days + 1`, then
    `while daysremaining < -5: daysremaining += 365` (`:268-273` and its four
    copies).

    The `+ 1` and the 365-day hop are both v1's, and both are load-bearing for
    what a group reads — see the module docstring's "The `+365` wraparound".
    """
    day, month, year = date
    remaining = (dt.datetime(year, month, day) - now).days + 1
    while remaining < -5:
        remaining += 365
    return remaining


def is_happening_now(remaining: int) -> bool:
    """v1: `if -5 <= daysremaining <= 0` (`:269`) — the window where the
    caption becomes a bare YouTube link instead of a countdown."""
    return -5 <= remaining <= 0


def render_caption(event: Event, remaining: int, cta: str) -> str:
    """The event's own f-string, with v1's substitutions. Note `day_end` is
    `day + days_span`, plain integer arithmetic on the *hardcoded* day: a
    convention starting on the 30th advertises "30 a 32/11" in v1 too."""
    day, month, year = event.date
    return event.caption.format(
        days=number_to_emojis(remaining),
        cta=cta,
        day=day,
        day_end=day + event.days_span,
        month=month,
        year=year,
    )


def caption_for(event: Event, now: dt.datetime, lang: str) -> str | None:
    """The full caption for one event at `now`, or `None` when the event has
    none at all (`/trex`, module docstring).

    `cta` is the one thing that does come from the locale catalog
    (`event.<name>.cta`, a list), drawn at random per call — v1's `:266`.
    """
    if not event.caption:
        return None
    remaining = days_remaining(event.date, now)
    if is_happening_now(remaining):
        return _HAPPENING_NOW
    return render_caption(event, remaining, _cta(event, lang))


def _cta(event: Event, lang: str) -> str:
    """One random line from `event.<name>.cta` (v1's `:266`).

    v1's `i18n.get` takes a dotted path; `locales.nested_value` resolves one
    level, so the per-event object comes back here and the `cta` list is read
    off it — the same "reach into a nested catalog at the call site" shape
    `groupguardian.py`'s `_captcha_strings` uses, for the same reason: this
    is the only two-level lookup in the codebase, and one caller does not
    justify a second lookup API.

    Only the `en` catalog carries the `event` object at all (the `pt`/`es`
    files have `event.error` and nothing else — v1's own state, ported
    verbatim), so `nested_value`'s fallback is what makes every language get
    the same Portuguese CTA lines. That is v1's behaviour, not a gap: see the
    module docstring on the caption's language.
    """
    section = locales.nested_value("event", event.cta_key, lang)
    lines = section.get("cta") if isinstance(section, dict) else None
    if not isinstance(lines, list) or not lines:
        return ""
    return cast(str, _rng.choice(lines))


# --------------------------------------------------------------------- handler


@router.message(F.chat.type != ChatType.PRIVATE, CommandName("con_patas"))
@router.message(F.chat.type != ChatType.PRIVATE, CommandName("con_bff"))
@router.message(F.chat.type != ChatType.PRIVATE, CommandName("con_fursmeet"))
@router.message(F.chat.type != ChatType.PRIVATE, CommandName("con_furcamp"))
@router.message(F.chat.type != ChatType.PRIVATE, CommandName("con_pawstral"))
@router.message(F.chat.type != ChatType.PRIVATE, CommandName("con_trex"))
async def event_countdown(message: Message, parsed: ParsedCommand | None = None) -> None:
    """One handler for all six triggers — v1's own shape: a single
    `event_countdown` function whose body is an `elif` chain over the command
    word (`Miscellaneous.py:261-323`). Six stacked filters rather than one
    multi-name filter, the same way `owner.py` registers `/stop` and
    `/restart` on one function.

    v1's trailing `else: event.error` (`:319-321`) is dead code — the only
    call site has already matched one of the five names — and has no
    equivalent here: `CommandName` cannot deliver a name `_BY_COMMAND` does
    not hold. The `event.error` string stays in the catalog, unread, exactly
    as unread as it is in v1.
    """
    if parsed is None:  # pragma: no cover - CommandName always injects a match
        return

    bot = cast(Bot, message.bot)
    ctx = await context_for(bot, message)

    canonical = parsed.name
    event = _BY_COMMAND.get(canonical)
    if event is None:  # pragma: no cover - CommandName only matches these six
        return

    # v1 reacts and signals "uploading a photo" before it knows which event it
    # is even looking at (`:262-263`); both best-effort, same as every other
    # reaction in this codebase.
    try:
        await message.react(reaction=[ReactionTypeEmoji(emoji="🔥")])
    except Exception as exc:  # noqa: BLE001 - a missing reaction must not cost the answer
        log.info("partneredcons.react_failed", error=str(exc))
    await bot.send_chat_action(message.chat.id, "upload_photo")

    entry = legacy_assets.choose(event.prefix, _rng)
    if entry is None:
        # `legacy-catalog` has never run in this deployment. v1's
        # `random.randint(0, -1)` raised here; `fun_death` established that
        # sending nothing is the better answer, and this follows it.
        log.warning("partneredcons.pool_empty", command=canonical, prefix=event.prefix)
        return

    data = await storage.store().get(entry.storage_key)
    _, extension = splitext(entry.source_path)
    photo = BufferedInputFile(data, filename=f"{canonical}{extension}")
    await message.reply_photo(photo, caption=caption_for(event, dt.datetime.now(), ctx.lang))


__all__ = [
    "Event",
    "caption_for",
    "days_remaining",
    "event_countdown",
    "is_happening_now",
    "render_caption",
    "router",
]
