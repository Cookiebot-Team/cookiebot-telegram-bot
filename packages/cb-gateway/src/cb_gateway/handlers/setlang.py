"""core_setlang — first-contact language derivation + per-chat command relabeling.

FEATURE-MAP row: `core_setlang`, status "spec says web UI, bot does in-chat menu"
(`docs/FEATURE-MAP.md` §1 and §5). QA: `../Cookiebot-QA/features/core_setlang.feature`
describes a **web settings page** — v1 has no such thing. v1's only language
selection surface is the in-chat `/config` menu's Language button, and that
button already lives in `handlers/config_menu.py` (callback letter `k`), owned by
another agent and out of scope here (task brief). See
`docs/contracts/core_setlang.md` for the full contract and the QA-vs-v1 conflict.

What v1 does *outside* the `/config` menu, ported here:

1. **First-contact language derivation** — `set_language`
   (`../COOKIEBOT-Telegram-Group-Bot/Bot/Configurations.py:242-251`), the only
   call site of which is `../COOKIEBOT-Telegram-Group-Bot/Bot/COOKIEBOT.py:133-134`,
   inside the "the bot itself was just added to a group" branch
   (`COOKIEBOT.py:121-135`). `derive_join_language` is the pure mapping;
   `on_bot_added_to_group` is the join-time trigger, since no existing v2 handler
   reacts to this event — `handlers/welcome.py:227-230` explicitly carves out and
   skips this exact case, citing the same v1 lines, deferring to "a separate
   bot-onboarding concern."
2. **`setMyCommands` relabeling** — `set_language_commands`
   (`Configurations.py:79-98`), called from two v1 sites: `set_private_commands`
   (`Configurations.py:100-101`, `/start` in a private chat — **not built here**,
   a private-chat onboarding concern, not a group-language concern) and
   `configurar_set`'s language branch (`Configurations.py:176-177`, the `/config`
   menu applying a language change — **not built here either**, since wiring that
   call is `handlers/config_menu.py`'s job and that file is out of scope for this
   port). `set_group_commands` is exposed here as the function whichever of those
   two owners can call; `apply_join_language` composes it with the write and the
   derivation for the one call site this port does own (the join event above).

Boundary — deliberately not reproduced (see contract doc for the full reasoning):

- v1's `COOKIEBOT.py:121-135` bundles four unrelated things into the same
  "bot added to group" branch: a blacklist/short-title auto-leave gate, a
  celebratory `sendAnimation`, an owner DM ("Added\n{chatinfo}"), and the
  language derivation. Only the last is `core_setlang`'s job; the other three
  belong to whichever feature owns onboarding/blacklist and are not touched by
  `on_bot_added_to_group` below. Consequence: today, in v2, this handler always
  derives a language on join — v1 would have skipped it entirely for a group
  that got auto-left. Once the blacklist/onboarding feature exists, it needs to
  short-circuit before this handler runs (or this handler needs to check the
  same gate) — flagged, not solved here.
- `set_private_commands`'s "private" branch (`Configurations.py:88-89,100-101`)
  is `/start`'s own command menu, not a group's — out of scope.
- The known v1 defect where `set_language_commands` currently sends an *empty*
  command list on every real invocation (`i18n.get_file` returns the whole file
  as one `str`, and v1's `for line in lines:` therefore iterates characters, not
  lines — confirmed against v1's git history, where a prior `.readlines()`-based
  version was refactored away without adding `.splitlines()`) is a silent-failure
  bug, not a user-visible quirk (AGENTS.md Phase 2: "Race conditions and
  silent-failure bugs get fixed"). `parse_manual_commands` below fixes it: it
  actually splits into lines before filtering, so `setMyCommands` receives the
  real command catalog instead of silently clearing the chat's command menu.

This module owns no other file. `handlers/__init__.py:build_router()` does not
include `setlang.router` (out of scope for this port; several features are being
ported in parallel against that file) — flagged for whoever wires it, along with
an ordering note: `welcome.py`'s `on_join` also matches `F.new_chat_members`
unconditionally and returns (without raising `SkipHandler`) for the bot's own
join, so if `welcome.router` is tried first, aiogram considers the update
already handled and `on_bot_added_to_group` below never runs. Whoever wires
`build_router()` needs `setlang.router` included *before* `welcome.router`, or
`welcome.py`'s early return changed to `raise SkipHandler()` — the same class of
cross-router ordering gap `docs/contracts/core_welcome.md`'s "Boundary" section
already flags for `core_groupguardian`/`util_doomlist`.
"""

from __future__ import annotations

from typing import cast

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand, BotCommandScopeChat, Message

from cb_core import group_config, locales
from cb_core.logging import get_logger

log = get_logger("cb.setlang")

router = Router(name="setlang")

# v1's exact naive substring check (Configurations.py:243-248) is case-sensitive
# and untrimmed — preserved verbatim, quirks included (see derive_join_language).
_COMMAND_SEPARATOR = " - "

# Configurations.py:91: the two languages *not* being switched to are relabeled
# first, then the target language last. The payload is identical for all three
# (see set_group_commands), so the order has no observable effect — kept for
# fidelity to the v1 call sequence rather than because it matters.
_COMMAND_MENU_LANGUAGE_CODES: tuple[str, ...] = ("pt", "es", "en")


# --------------------------------------------------------------------- pure logic


def derive_join_language(language_code: str | None) -> str | None:
    """v1's `set_language` mapping, `Configurations.py:243-248`, verbatim:

        if 'pt' in language_code: "pt"
        elif 'es' in language_code: "es"
        else: "eng"

    Two things are preserved exactly, not fixed:

    - **Case-sensitive substring match, not equality.** `'pt' in "pt-BR"` is
      `True` (a real Telegram code), but so is `'pt' in "chapter"` — any code
      that happens to *contain* the two-letter substring anywhere matches, not
      just a leading subtag. `'PT'`/`'ES'` (uppercase) do **not** match either
      branch, since v1 never lowercases `language_code` — an uppercase-tagged
      client falls through to the `else` branch, same as v1.
    - **The literal three-way output** (`"pt"` / `"es"` / `"eng"`), not the
      canonical `"pt"`/`"es"`/`"en"` `cb_core.locales` uses internally.
      `group_configs.language` stores v1's raw strings verbatim (see
      `docs/contracts/group-config.md`; `qa/integration/test_config_menu.py`
      already asserts this for the `/config` menu's own write path) — a fresh
      group created by this path must look identical in the database to one
      created by v1, not merely resolve to the same displayed language.

    v1 only ever calls this when `'language_code' in msg['from']`
    (`COOKIEBOT.py:133`) — i.e. the *key* is present, whether or not its value
    is a non-empty string. `None` here means "Telegram sent no `language_code`
    at all" (the key was absent) and returns `None`, so the caller knows to
    leave the group's language completely untouched rather than writing a
    value — matching v1, which simply never calls `set_language` in that case.
    An empty string is treated as *present* (matching v1's `in` check on the
    dict key, not on the value) and flows through to the `else` branch, exactly
    as v1's `'pt' in ""` (`False`) would.
    """
    if language_code is None:
        return None
    if "pt" in language_code:
        return "pt"
    if "es" in language_code:
        return "es"
    return "eng"


def parse_manual_commands(catalog_text: str) -> list[BotCommand]:
    """v1's inline parser, `Configurations.py:82-87`, fixed to actually split
    into lines (see module docstring for the confirmed defect this replaces).

    Keeps only single-word, all-lowercase `command - description` rows — the
    "MANUAL COMMANDS" block of `Cookiebot_functions.txt` — excluding the
    "AUTOMATIC FEATURES" section, whose entries are capitalized
    (`"Publisher - ..."`) and therefore fail `.islower()`, exactly as in v1.
    """
    commands: list[BotCommand] = []
    for line in catalog_text.splitlines():
        if _COMMAND_SEPARATOR not in line:
            continue
        # v1: line.split(" - ")[0]/[1] — an unbounded split, not partition, so a
        # description containing a second " - " would lose everything after it.
        # No real catalog line does; reproduced anyway for byte-for-byte fidelity.
        parts = line.split(_COMMAND_SEPARATOR)
        name = parts[0].strip()
        description = parts[1].replace("\n", "")
        if len(name.split()) == 1 and name.islower():
            commands.append(BotCommand(command=name, description=description))
    return commands


def _confirmation_text(canonical_language: str, group_id: int) -> str:
    """`Configurations.py:97`, verbatim. Only three literal branches exist in
    v1 — any language that is not `pt`/`es` gets the English wording, so a
    resolved-but-unexpected `canonical_language` still produces a sensible
    (English) string instead of nothing, matching v1's `if/elif/else` shape."""
    if canonical_language == "pt":
        return f"Comandos no chat com ID <b> {group_id} </b> alterados para o idioma <b> Português </b>"
    if canonical_language == "es":
        return f"Comandos en el chat con ID <b> {group_id} </b> cambiados a idioma <b> Español </b>"
    return f"Commands in chat with ID <b> {group_id} </b> changed to language <b> English </b>"


# ------------------------------------------------------------------- side effects


async def set_group_commands(
    bot: Bot,
    group_id: int,
    language: str,
    *,
    notify_chat_id: int | None = None,
    silent: bool = False,
) -> bool:
    """Relabel `group_id`'s Telegram command menu. v1: `set_language_commands`,
    `Configurations.py:79-98` (the group-scope `else` branch only — the
    `language == "private"` branch is `/start`'s own menu, out of scope here).

    v1 always targets `BotCommandScopeChat` (never the default or
    all-private-chats scope, `universal_funcs.py:280-289`) and relabels the
    *same* target-language command list under all three Telegram
    `language_code` scopes it supports (`pt`, `es`, `en` — v1's
    `language[0:2].lower()` applied to its own `"pt"`/`"es"`/`"eng"` strings),
    so the chat's command menu reads in the group's chosen language regardless
    of which of those three UI languages the *viewer's own* Telegram client
    happens to be set to.

    Failure policy (the thing v1 never actually decided — its own
    `set_bot_commands` call is wrapped in a retry decorator that only re-raises
    genuinely transient network errors and otherwise just returns Telegram's raw
    response text, which nothing downstream inspects): a rejected `setMyCommands`
    call is logged and does **not** raise — the language change that triggered
    it (`group_config.set_config`, already committed by the time this runs) must
    not be undone or reported as failed just because the cosmetic command-menu
    relabeling didn't land. Returns whether every scope's call succeeded, for
    callers that want to know without catching anything themselves.
    """
    canonical = locales.resolve_language(language)
    commands = parse_manual_commands(locales.text("Cookiebot_functions", canonical))
    ok = True
    for code in _COMMAND_MENU_LANGUAGE_CODES:
        try:
            await bot.set_my_commands(
                commands, scope=BotCommandScopeChat(chat_id=group_id), language_code=code
            )
        except TelegramAPIError as exc:
            ok = False
            log.warning(
                "setlang.set_my_commands_failed",
                group_id=group_id,
                language_code=code,
                error=str(exc),
            )

    if ok and not silent and notify_chat_id is not None:
        try:
            await bot.send_message(notify_chat_id, _confirmation_text(canonical, group_id))
        except TelegramAPIError as exc:
            log.info("setlang.confirmation_failed", group_id=group_id, error=str(exc))

    return ok


async def apply_join_language(
    bot: Bot,
    group_id: int,
    language_code: str | None,
    *,
    notify_chat_id: int | None = None,
) -> str | None:
    """The composed helper the group-registration path can call: derive, write,
    relabel. v1: `set_language`, `Configurations.py:242-251`, fully composed
    (v1's version funnels through `configurar_set`'s generic reply-apply path;
    here the three steps are direct, since there is no reply to simulate).

    Returns the literal value written to `group_configs.language` (`"pt"` /
    `"es"` / `"eng"`), or `None` if Telegram gave no `language_code` at all and
    nothing was changed — mirrors v1's own no-op in that case.
    """
    language = derive_join_language(language_code)
    if language is None:
        return None
    await group_config.set_config(group_id, language=language)
    await set_group_commands(bot, group_id, language, notify_chat_id=notify_chat_id)
    return language


# --------------------------------------------------------------------- join event


@router.message(F.new_chat_members)
async def on_bot_added_to_group(message: Message) -> None:
    """The bot's own join. v1: `COOKIEBOT.py:121-135`, narrowed to the
    language-derivation slice only — see the module docstring's "Boundary"
    section for what is deliberately not reproduced (the blacklist/short-title
    auto-leave gate, the celebratory animation, the owner DM), and for the
    router-ordering dependency this handler has on `welcome.router`.
    """
    joiners = message.new_chat_members
    if not joiners:
        raise SkipHandler
    # Same v1 quirk `welcome.py` documents: only the first joiner in a batch is
    # ever inspected. Irrelevant in practice here since a "the bot joined" event
    # is always a batch of one, but kept consistent with the sibling handler.
    newcomer = joiners[0]
    bot = cast(Bot, message.bot)
    if newcomer.id != bot.id:
        # A human joining is the welcome/captcha/doomlist chain's business.
        raise SkipHandler

    language_code = message.from_user.language_code if message.from_user else None
    try:
        await apply_join_language(bot, message.chat.id, language_code)
    except Exception as exc:  # noqa: BLE001 - a first-contact hiccup must not crash the join event
        log.warning("setlang.join_derivation_failed", group_id=message.chat.id, error=str(exc))


__all__ = [
    "apply_join_language",
    "derive_join_language",
    "on_bot_added_to_group",
    "parse_manual_commands",
    "router",
    "set_group_commands",
]
