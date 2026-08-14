"""x_image_search — `/qualquercoisa` prints a usage line, and **every
unrecognised `/command` is a Google image search**.

v1: `prompt_qualquer_coisa` and `qualquer_coisa`,
`../COOKIEBOT-Telegram-Group-Bot/Bot/SocialContent.py:144-170`, dispatched
`COOKIEBOT.py:258-259` (the prompt) and `:283-289` (the catch-all). Contract:
`docs/contracts/x_image_search.md`. Spec/design:
`.specs/features/x_image_search/`.

## Two commands that look like one

`/qualquercoisa`, `/anything`, `/cualquiercosa` do **not** search anything.
They print `anything_prompt` — "you need to type the name of what you want to
search for, EXAMPLE: /french fries" — because the real feature is the last
`elif` in v1's command chain: any `/word` no handler claimed becomes a search
for that word. `/french fries` is not a command with an argument, it is the
query.

That makes this the one handler in the codebase whose trigger is "no other
trigger matched" — hence a router registered after every command router, and
hence `SkipHandler` on both non-matches, since three real commands are
registered even later for reasons of their own (see `catch_all_search`).

## What v1 checks before searching, in order

1. `utilityfunctions` (`COOKIEBOT.py:283`). Note the shape: this is the final
   `elif` of the chain, so a group with utility **off** gets *silence* here,
   not the `utility_off` reply its siblings send. Preserved — see
   `deny_if_disabled`'s absence below.
2. `"//" not in msg['text']` — a URL pasted with no scheme (`example.com//x`)
   or a doubled slash is not a search.
3. The command is not addressed at another bot: `len(text.split('@')) < 2 or
   text.split('@')[1] in [the five persona usernames]`. v2 asks the registry
   for the current bot's username instead of a hardcoded list, so a sixth
   brand works without a code change; the observable rule is the same.
4. The daily quotas, **decremented before the check** (`:284-285`), so the
   call that crosses the limit is the one that gets refused.
5. The blocklist, inside `qualquer_coisa` (`:149-150`) and therefore *after*
   the quota has already been spent. Preserved deliberately: it is the
   difference between "typing `/etc` costs you a search" and not, and it is
   what v1 does.

## The quota is now shared, which is what the number always meant

v1 counts in a per-process dict (`Cooldowns.py:38-47`), so the "180 searches a
day" global cap was really 180 *per process*, times five processes, and reset
whenever one restarted. Here both counters are Valkey keys with a one-day
window (`cache.incr_window`, the same primitive `core_stickerspam` uses), so
the cap means what it says across every replica. A Valkey outage fails open —
the search runs — the same direction every other cache miss in this codebase
fails, because refusing every search during a cache blip would be the worse
failure.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.enums import ChatType
from aiogram.types import Message

from cb_core import cache, jobs
from cb_core import image_search as core_image_search
from cb_core.logging import get_logger
from cb_core.settings import get_settings
from cb_core.textmatch import ParsedCommand, parse_command
from cb_gateway.context import context_for, t
from cb_gateway.filters import CommandName
from cb_gateway.queue import enqueue
from cb_gateway.telemetry import mark_outcome

log = get_logger("cb.gateway.image_search")

router = Router(name="image_search")

#: One UTC day. v1 keys its dict on `datetime.datetime.now().date()`, i.e. the
#: host's local date; the containers run UTC, and a fixed-length window is what
#: `incr_window` can express atomically.
_DAY_SECONDS = 86_400


def quota_keys(user_id: int, *, now: datetime | None = None) -> tuple[str, str]:
    """`(per-user key, global key)` for today. The date is in the key rather
    than a stored field so the window rolls over on its own, where v1 compared
    a stored `date` on every call (`Cooldowns.py:40,44`)."""
    day = (now or datetime.now(UTC)).strftime("%Y%m%d")
    return f"cb:imgsearch:u:{user_id}:{day}", f"cb:imgsearch:all:{day}"


async def within_quota(user_id: int) -> bool:
    """v1: `decrease_remaining_image_searches(...)` then
    `remaining[user] >= 0 and remaining['total'] >= 0` (`COOKIEBOT.py:284-285`).

    Both counters are spent before either is checked — including when the
    *other* one is what refuses — so a user who has run out still consumes the
    bot's global budget. That is v1's own arithmetic, not an accident of this
    port, and it is why both `incr` calls happen before the comparison.

    Fails open on a cache outage (module docstring).
    """
    settings = get_settings()
    user_key, total_key = quota_keys(user_id)
    try:
        used_by_user = await cache.incr_window(user_key, _DAY_SECONDS)
        used_in_total = await cache.incr_window(total_key, _DAY_SECONDS)
    except Exception as exc:  # noqa: BLE001 - a cache outage must not refuse every search
        log.warning("image_search.quota_unavailable", error=str(exc))
        return True
    return (
        used_by_user <= settings.image_search_daily_per_user
        and used_in_total <= settings.image_search_daily_total
    )


def is_search_candidate(text: str, bot_username: str) -> bool:
    """v1's two guards on the catch-all `elif` (`COOKIEBOT.py:283`), plus the
    `startswith("/") and len(text) > 1` its enclosing `if` already applied
    (`:186`).

    `"//" not in text` drops a pasted `https://…` or `example.com//x`; the
    `@`-check drops a command explicitly addressed at a different bot, which
    Telegram delivers to every bot in the group. v1 compared against five
    hardcoded persona usernames, so a sixth brand would have had to edit the
    list; this compares against the bot the update actually arrived on.
    """
    if not text.startswith("/") or len(text) <= 1:
        return False
    if "//" in text:
        return False
    parts = text.split("@")
    if len(parts) < 2:
        return True
    target = parts[1].split()[0] if parts[1].split() else ""
    return bool(bot_username) and target.lower() == bot_username.lower()


@router.message(F.chat.type != ChatType.PRIVATE, CommandName("anything"))
async def anything_prompt(message: Message, parsed: ParsedCommand | None = None) -> None:
    """`/qualquercoisa`, `/anything`, `/cualquiercosa` — the usage line, not a
    search (`SocialContent.py:144-146`). No gate: v1 reaches
    `prompt_qualquer_coisa` from the `utilityfunctions`-gated stretch
    (`COOKIEBOT.py:253,258-259`), so utility-off answers `utility_off` here,
    unlike the catch-all below."""
    ctx = await context_for(cast(Bot, message.bot), message)
    if not ctx.enabled("utility"):
        mark_outcome("refused")
        await message.reply(t(ctx, "utility_off"))
        return
    await message.reply(t(ctx, "anything_prompt"))


@router.message(F.chat.type != ChatType.PRIVATE, F.text.startswith("/"))
async def catch_all_search(message: Message, bot_username: str = "") -> None:
    """Every `/word` no other handler claimed (`COOKIEBOT.py:283-289`).

    Registered after every command router, but **not** after every router:
    three handlers that own real commands are registered further down for
    reasons of their own — `welcome`'s `/newwelcome` reply prompt sits in the
    join chain, and `transcribe` and `fun_random` sit in the content-rules
    block because each also has a passive half. So "no other trigger matched"
    cannot be inferred from position alone, and both non-matches below raise
    `SkipHandler` rather than returning: a handler that returns has *handled*
    the update, and aiogram stops there (`handlers/__init__.py`'s module
    docstring says the same thing about the join chain).
    """
    text = message.text or ""
    if not is_search_candidate(text, bot_username):
        raise SkipHandler
    if parse_command(text, bot_username) is not None:
        # A real command. Either its handler already ran and this line is
        # unreachable, or it is one of the three registered below — in which
        # case turning it into an image search would delete the feature.
        raise SkipHandler

    ctx = await context_for(cast(Bot, message.bot), message)
    if not ctx.enabled("utility"):
        # Silence, not `utility_off`: this is the last elif of v1's chain, so
        # a utility-off group falls off the end of it (module docstring).
        mark_outcome("silent")
        return

    sender = message.from_user
    if sender is None:  # pragma: no cover - a group message always carries one
        return
    if not await within_quota(sender.id):
        await message.reply(t(ctx, "image_limit"))
        return

    term = core_image_search.search_term(text)
    if core_image_search.is_avoided(term):
        # v1 returns silently from inside `qualquer_coisa` (`:149-150`) —
        # after the quota was already spent, which is preserved above.
        mark_outcome("silent")
        return

    await enqueue(
        jobs.IMAGE_SEARCH,
        group_id=ctx.group_id,
        message_id=message.message_id,
        query=term,
        # v1: `safe='off'` when the group is not SFW, `'medium'` when it is
        # (`SocialContent.py:153-156`).
        safe="medium" if ctx.config.sfw else "off",
        lang=ctx.lang,
    )


__all__ = [
    "anything_prompt",
    "catch_all_search",
    "is_search_candidate",
    "quota_keys",
    "router",
    "within_quota",
]
