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

from cb_core import legacy_assets
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


class CustomCommandName(BaseFilter):
    """Matches a command whose name is a **pool**, not a canonical alias.

    v1's `Custom/` commands have no place in `COMMAND_ALIASES` and cannot get
    one: their names are folder names in a bucket
    (`Miscellaneous.py:23,147`), the pool is data rather than code, and
    `AGENTS.md` §2.1's "no new command name without an alias" is about not
    dropping a v1 trigger — these are v1 triggers precisely *because* the data
    says so. `parse_command` therefore returns `None` for them, which is why
    this filter reads the head itself.

    Head extraction deliberately mirrors `parse_command`'s
    (`textmatch.py:161-181`) rather than v1's own
    `text.replace('/', '').replace('@CookieMWbot', '').split()[0]`: v1's chain
    strips *every* slash anywhere in the word and knows only two of its own
    five bot usernames, so `/foo/bar` and `/foo@SCTarinBot` resolved to
    `foobar` and `foo@SCTarinBot`. Neither reaches a real folder name, so
    neither is a behaviour a group can have depended on; matching the parser
    the rest of the codebase uses is what keeps `@`-addressed commands working
    for every skin.

    Injects `custom` — `(name, args)` — into the handler.
    """

    async def __call__(
        self,
        message: Message,
        bot_username: str = "",
    ) -> bool | dict[str, tuple[str, str]]:
        text = message.text or ""
        if not text.startswith("/"):
            return False
        head, _, rest = text.partition(" ")
        name = head[1:]
        at = name.find("@")
        if at >= 0:
            target = name[at + 1 :]
            name = name[:at]
            if target and bot_username and target.lower() != bot_username.lower():
                return False
        name = name.lower()
        if not name or not legacy_assets.entries_for_custom(name):
            return False
        return {"custom": (name, rest.strip())}


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
