"""Command filter backed by the compiled parser.

v1 dispatched with a ~250-line if/elif chain over raw text, re-evaluated per
message (`COOKIEBOT.py:185-316`). Here the text is parsed once in middleware and
every filter is a string compare.
"""

from __future__ import annotations

from typing import cast

from aiogram import Bot
from aiogram.filters import BaseFilter
from aiogram.types import Message

from cb_core.textmatch import ParsedCommand, parse_command


class FeatureGate(BaseFilter):
    """v1's `functionsFun` / `functionsUtility` switches, for **passive** features.

    v1 treats the two cases differently, and the difference is user-visible:

    * A gated-off **command** replies. `COOKIEBOT.py:218` and `:252` call
      `notify_fun_off` / `notify_utility_off`, which send the catalog strings
      `fun_off` / `utility_off`. Silence there is a regression — use
      `cb_gateway.context.deny_if_disabled()` in the handler instead of this
      filter, so the user learns why nothing happened.
    * A gated-off **passive** feature says nothing. The link embedder is dispatched
      under a bare `if utilityfunctions:` (`COOKIEBOT.py:311`) with no else branch,
      because a message that merely contains a link never asked for anything.

    This filter is the second case. An earlier version of this docstring claimed
    v1 never replies at all, which is why it is spelled out here.
    """

    def __init__(self, area: str) -> None:
        if area not in {"fun", "utility"}:
            raise ValueError(f"unknown feature area {area!r}")
        self.area = area

    async def __call__(self, message: Message, bot: Bot | None = None) -> bool:
        from cb_gateway.context import context_for

        ctx = await context_for(cast(Bot, bot or message.bot), message)
        return ctx.enabled(self.area)


class AdminOnly(BaseFilter):
    """Admin-gated commands, anonymous admins included.

    v1 compared `from.id` against a cached list, which an anonymous admin can
    never appear in — see `cb_core/admins.py` and docs/contracts/admins.md. The
    resolution lives there; this filter only routes on it.
    """

    async def __call__(self, message: Message, bot: Bot | None = None) -> bool:
        from cb_gateway.context import context_for

        ctx = await context_for(cast(Bot, bot or message.bot), message)
        return ctx.is_admin


class CommandName(BaseFilter):
    """Matches a canonical command name, whatever alias the user typed."""

    def __init__(self, name: str) -> None:
        self.name = name

    async def __call__(
        self,
        message: Message,
        parsed_command: ParsedCommand | None = None,
        bot_username: str = "",
    ) -> bool | dict[str, ParsedCommand]:
        parsed = parsed_command or parse_command(message.text or "", bot_username)
        if parsed is None or parsed.name != self.name:
            return False
        return {"parsed": parsed}
