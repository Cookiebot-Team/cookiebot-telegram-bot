"""util_config — the in-chat admin configuration menu.

v1's entire flow lives in one file (`../COOKIEBOT-Telegram-Group-Bot/Bot/Configurations.py`):
`configurar` (`:139-167`) is the `/configurar`/`/configure` entry point, `config_variable_button`
(`:213-240`) answers a menu button press with a prompt, and `configurar_set` (`:169-211`) applies
the admin's reply. `cb_core.textmatch.COMMAND_ALIASES` already maps `configurar`/`configure`/`config`
to the canonical `config` command, so both v1's trigger and QA's spelling work with no change here.

Design differences from v1, each recorded in `docs/contracts/util_config.md`:

- **The anonymous-admin bug is fixed, not reproduced.** v1 (`Configurations.py:141`) checks
  `str(from_id) not in listaadmins_id`, and an anonymous admin's `from.id` is Telegram's synthetic
  `GroupAnonymousBot`, so a genuine admin posting anonymously was always rejected and sent a tutorial
  video about turning off a Telegram feature that was never the problem. `ctx.is_admin` (via
  `cb_core.admins.resolve_actor`) already treats an anonymous sender as an admin. But Telegram gives
  us no real user id to open a DM with in that case (`ctx.actor.user_id is None`), so an anonymous
  admin takes the same graceful branch v1 uses when a DM to a *known* admin fails — "couldn't reach
  you privately" — instead of the old rejection-plus-video message.
- **Every callback_query is answered.** v1 never calls `answerCallbackQuery` for a `CONFIG` callback
  (`COOKIEBOT.py:360-361`), so pressing a menu button in a real Telegram client spins forever. Both
  handlers below answer unconditionally.
- **No manual `/reload` instruction.** v1's success message tells the admin to "Send /reload in the
  chat if the old config persists" (`Configurations.py:209`) because its cache is an unbounded
  per-process dict with five independent copies (FEATURE-MAP D6). `group_config.set_config` already
  invalidates every replica via pub/sub, so the instruction would be actively wrong advice here.
- **Bad input no longer crashes.** v1's numeric fields call `int(new_val)` uncaught
  (`Configurations.py:179-201`); a non-numeric reply propagates to `thread_function`'s top-level
  `except Exception` (silent to the user, a traceback mailed to the bot owner). v2 catches the parse
  failure and reuses v1's own "ERROR: invalid input\nTry again" text (already the message v1 sends
  for an empty reply, `Configurations.py:211`), just for every parse failure, not only the empty one.
- **The `setMyCommands` side effect of changing `language` is reproduced, by calling the function
  `core_setlang` wrote for it.** v1's `set_language` path also relabels the group's Telegram command
  menu in three languages (`Configurations.py:79-98,176-177`). This module's port declared it out of
  scope because the command menu is `core_setlang`'s concern rather than a `group_configs` write —
  right about the implementation, which is why `setlang.set_group_commands` exists and is tested, but
  it left the side effect with no caller at all: an admin who changed the language here saw every
  reply switch and the command menu stay in the old one. `apply_language_side_effect` below is the
  call, and it is never fatal — the write has already landed and been confirmed.
- **No locale catalog entries exist yet for this menu.** v1's menu text, prompts and per-field labels
  are hardcoded English regardless of group language (only the three group-facing confirmation/error
  strings are machine-translated at send time via `translate()`, `universal_funcs.py:139-163`, an
  external Google Translate call this port does not reproduce). `cb_core.locales` has no keys for any
  of `util_config`'s strings (checked: no `config`-prefixed key in any `lib.json`), and this module may
  not add any (cb_core/* is out of scope for this port) — the three group-facing strings below are
  hand-translated literals, not a `locales.get()` lookup. Reported to the catalog owner.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import Literal

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReactionTypeEmoji,
)

from cb_core import errors, group_config, locales
from cb_core.group_config import GroupConfig
from cb_core.logging import get_logger
from cb_core.settings import get_settings
from cb_core.telemetry import current_trace_id
from cb_gateway.context import context_for
from cb_gateway.filters import CommandName
from cb_gateway.handlers import setlang
from cb_gateway.telemetry import error_reason_for_chat, mark_outcome

log = get_logger("cb.config_menu")

router = Router(name="config_menu")

# v1's magic substring, matched verbatim against a reply's `reply_to_message.text`
# (`Configurations.py:174` / `config_variable_button`'s prompts) to recognise "this
# reply answers one of our config prompts" without any FSM/state store.
_MAGIC_MARKER = "REPLY THIS MESSAGE with the new variable value"

# v1 answers the rejection branch with `Static/remove_anonymous_tutorial.mp4`, read
# off the bot's own filesystem and re-uploaded on every rejection
# (`Configurations.py:143`); `util_config.feature:13` requires it too. v2 sends the
# same video as a Telegram file_id (`CB_ANONYMOUS_TUTORIAL_FILE_ID`) instead of
# re-uploading, and sends nothing when no asset is configured — a deployment
# without the video must not be handed a URL that may not resolve.
#
# Note that this branch is now narrower than v1's: an anonymous admin no longer
# lands here at all (docs/contracts/admins.md), so the video only ever reaches
# someone who genuinely is not an admin.

FieldKind = Literal["bool", "int", "topic", "language"]


@dataclass(frozen=True, slots=True)
class ConfigField:
    """One row of the menu: a callback letter, its button label, the
    `group_configs` column it writes, how to parse a reply into that column's
    type, and the exact prompt text v1 sends for it."""

    letter: str
    label: str
    column: str
    kind: FieldKind
    prompt: str


# Exact v1 order, letters, labels and prompts — `configurar`'s inline_keyboard
# (`Configurations.py:150-163`) and `config_variable_button`'s prompt strings
# (`:215-240`). Do not reorder: the order here is the order buttons render in.
CONFIG_FIELDS: tuple[ConfigField, ...] = (
    ConfigField(
        "k",
        "Language",
        "language",
        "language",
        "Bot language for the chat. Use pt for portuguese, eng for english or es for spanish",
    ),
    ConfigField(
        "a",
        "FurBots",
        "allow_furbots",
        "bool",
        "Use 1 to not interfere with other furbots if they're in the group, or 0 if I'm the only one.",
    ),
    ConfigField(
        "b",
        "Stickers limit",
        "sticker_spam_limit",
        "int",
        "This is the maximum number of stickers allowed in a sequence by the bot. The next ones "
        "beyond that will be deleted to avoid spam. It's valid for everyone.",
    ),
    ConfigField(
        "c",
        "🕒 Limbo",
        "media_restrict_seconds",
        "int",
        "This is the time for which new users in the group will not be able to send images "
        "(the bot automatically deletes).",
    ),
    ConfigField(
        "d",
        "🕒 CAPTCHA",
        "captcha_timeout_seconds",
        "int",
        "This is the time new users have to solve Captcha. USE 0 TO TURN CAPTCHA OFF!",
    ),
    ConfigField(
        "h",
        "Fun Functions",
        "functions_fun",
        "bool",
        "Use 1 to enable commands and fun functionality, or 0 for control/management functions only.",
    ),
    ConfigField(
        "i",
        "Utility Functions",
        "functions_utility",
        "bool",
        "Use 1 to enable commands and utility features, or 0 to disable them.",
    ),
    ConfigField(
        "j",
        "SFW Chat",
        "sfw",
        "bool",
        "Use 1 to indicate the chat is SFW, or 0 for NSFW.",
    ),
    ConfigField(
        "m",
        "Publisher Post",
        "publisher_post",
        "bool",
        "Use 1 to allow the bot to post publications from other channels (only works if group has "
        "over 50 members), or 0 to not allow",
    ),
    ConfigField(
        "n",
        "Publisher Ask",
        "publisher_ask",
        "bool",
        "Use 1 if the bot should add posts sent in the group to the publisher queue, or 0 if not",
    ),
    ConfigField(
        "o",
        "Thread Posts",
        "thread_posts",
        "topic",
        "This is the id of the topic I should publish posts to if your chat has topics enabled "
        "(you can find it out with /analysis command)",
    ),
    ConfigField(
        "p",
        "Max Posts",
        "max_posts",
        "int",
        "This is the maximum number of posts I should publish in the chat per day",
    ),
    ConfigField(
        "q",
        "Publisher Members Only",
        "publisher_members_only",
        "bool",
        "Use 1 if the bot should only allow members of the channel to use the publisher, or 0 if not",
    ),
)

FIELD_BY_LETTER: dict[str, ConfigField] = {field.letter: field for field in CONFIG_FIELDS}

# The three group-facing strings v1 machine-translates at send time (`translate()`,
# `universal_funcs.py:139-163`, a live Google Cloud Translate call this port does not
# reproduce — see module docstring). English matches the QA spec text verbatim;
# Portuguese is v1's literal source string (`Configurations.py:142,165,167`); Spanish
# is a hand translation, not `translate()`'s actual output.
_DENIED_TEXT = {
    "pt": (
        "Você não tem permissão para configurar o bot, ou está anônimo!\n"
        "<blockquote> Você está falando como usuário e não como canal? A permissão "
        "'permanecer anônimo' deve estar desligada! </blockquote>"
    ),
    "en": (
        "You don't have permission to use this command or are in anonymous mode\n"
        "<blockquote> Are you speaking as a user and not as a channel? The 'remain anonymous' "
        "permission should be turned off! </blockquote>"
    ),
    "es": (
        "¡No tienes permiso para configurar el bot, o estás en modo anónimo!\n"
        "<blockquote> ¿Estás hablando como usuario y no como canal? ¡El permiso 'permanecer "
        "anónimo' debe estar desactivado! </blockquote>"
    ),
}

_SENT_DM_TEXT = {
    "pt": "Te mandei uma mensagem no chat privado para configurar!",
    "en": "I've sent you a message in the private chat to configure!",
    "es": "¡Te envié un mensaje en el chat privado para configurar!",
}

_CANNOT_DM_TEXT = {
    "pt": (
        "Não consegui te mandar o menu de configuração\n"
        "<blockquote> Mande uma mensagem no meu chat privado para que eu consiga fazer isso) "
        "</blockquote>"
    ),
    "en": (
        "I couldn't send you the configuration menu\n"
        "<blockquote> Send me a message in my private chat so I can do that) </blockquote>"
    ),
    "es": (
        "No pude enviarte el menú de configuración\n"
        "<blockquote> Envíame un mensaje en mi chat privado para que pueda hacerlo) </blockquote>"
    ),
}

_INVALID_INPUT_TEXT = "ERROR: invalid input\nTry again"
_SUCCESS_TEXT = "Successfully changed the variable!"
# No v1 counterpart: v1 had no failure path here at all, it simply crashed and the
# admin was told nothing. Phrased in the same register as the two strings above.
_WRITE_FAILED_TEXT = "ERROR: could not save the setting\nTry again in a moment"


def _text_for(table: dict[str, str], lang: str) -> str:
    return table.get(lang, table["en"])


# --------------------------------------------------------------- pure helpers


def build_callback_data(letter: str, group_id: int) -> str:
    """v1's exact wire shape (`Configurations.py:150-163`): `"{letter} CONFIG {chat_id}"`."""
    return f"{letter} CONFIG {group_id}"


def parse_callback_data(data: str) -> tuple[str, int] | None:
    """The inverse of `build_callback_data`, `None` for anything malformed or unknown."""
    parts = data.split()
    if len(parts) != 3 or parts[1] != "CONFIG":
        return None
    letter = parts[0]
    if letter not in FIELD_BY_LETTER:
        return None
    try:
        group_id = int(parts[2])
    except ValueError:
        return None
    return letter, group_id


def build_menu_keyboard(group_id: int) -> InlineKeyboardMarkup:
    """One button per row, in `CONFIG_FIELDS` order — v1's exact layout."""
    rows = [
        [
            InlineKeyboardButton(
                text=field.label, callback_data=build_callback_data(field.letter, group_id)
            )
        ]
        for field in CONFIG_FIELDS
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_prompt(field: ConfigField, group_id: int) -> str:
    """v1's exact prompt shape (`config_variable_button`, `Configurations.py:213-240`):
    `"Chat = {chat}\\n{prompt}\\n\\nREPLY THIS MESSAGE with the new variable value"`."""
    return f"Chat = {group_id}\n{field.prompt}\n\n{_MAGIC_MARKER}"


def extract_group_id(prompt_text: str) -> int | None:
    """Recovers the target group id from a prompt's first line, `"Chat = {id}"`."""
    first_line = prompt_text.splitlines()[0] if prompt_text else ""
    if not first_line.startswith("Chat = "):
        return None
    try:
        return int(first_line.removeprefix("Chat = ").strip())
    except ValueError:
        return None


def find_field_by_prompt(prompt_text: str) -> ConfigField | None:
    """Which field a prompt belongs to — v1's if/elif substring chain
    (`configurar_set`, `Configurations.py:174-201`), reproduced as a lookup."""
    for field in CONFIG_FIELDS:
        if field.prompt in prompt_text:
            return field
    return None


def parse_reply_value(field: ConfigField, text: str) -> object | None:
    """Coerce a reply into the type `group_config.set_config` expects for `field.column`.

    `None` means "reject with the invalid-input message" — v1's behaviour for an
    empty reply (`Configurations.py:210-211`), extended here to cover a reply that
    fails to parse as the expected type instead of letting the exception escape
    uncaught (`Configurations.py:179-201` has no `try`/`except` at all).
    """
    new_val = text.strip().lower()
    if not new_val:
        return None
    if field.kind == "language":
        # v1 does not validate this beyond non-empty either (`Configurations.py:174`,
        # the `if new_val or new_val in [...]` condition is always true for any
        # non-empty string) — preserved rather than fixed, since a bad language
        # value degrades harmlessly through `cb_core.locales.resolve_language`.
        return new_val
    if field.kind == "bool":
        try:
            return bool(int(new_val))
        except ValueError:
            return None
    if field.kind == "int":
        try:
            return int(new_val)
        except ValueError:
            return None
    if field.kind == "topic":
        try:
            return str(int(new_val))
        except ValueError:
            return None
    raise AssertionError(f"unhandled field kind {field.kind!r}")  # pragma: no cover


def menu_text(config: GroupConfig) -> str:
    """v1's "Current settings" block, verbatim (`configurar`, `Configurations.py:147-149`).

    Note `publisher_members_only` is deliberately absent from the summary even
    though it has a menu button ('q') — v1's `variables` string stops at
    `configs[11]` (Max Posts) and never prints `configs[12]`. Reproduced exactly,
    not fixed: a cosmetic summary gap, not a functional one (the button and prompt
    for it both work).
    """
    variables = (
        f"FurBots: {config.allow_furbots}\n"
        f" sfw: {config.sfw}\n"
        f" Sticker Spam Limit: {config.sticker_spam_limit}\n"
        f" Time Without Sending Images: {config.media_restrict_seconds}\n"
        f" Time Captcha: {config.captcha_timeout_seconds}\n"
        f" Fun Functions: {config.functions_fun}\n"
        f" Utility Functions: {config.functions_utility}\n"
        f" Language: {config.language}\n"
        f" Publisher Post: {config.publisher_post}\n"
        f" Publisher Ask: {config.publisher_ask}\n"
        f" Thread Posts: {config.thread_posts}\n"
        f" Max Posts: {config.max_posts}"
    )
    return (
        "Current settings:\n\n" + variables + "\n\nChoose the variable you would like to change\n\n"
        "(If you want to change rules or welcome message, use /newrules or /newwelcome on the group)"
    )


async def apply_change(group_id: int, field: ConfigField, value: object) -> GroupConfig:
    """The write itself, isolated from Telegram object parsing.

    Split out so integration tests can drive it directly against a real database
    without constructing full aiogram `Message`/`CallbackQuery` objects — see
    `qa/integration/test_config_menu.py`.
    """
    return await group_config.set_config(group_id, **{field.column: value})


async def apply_language_side_effect(bot: Bot, group_id: int, language: object) -> None:
    """Relabel the group's Telegram command menu after a language change.

    v1 does this on the same path (`set_language` → `set_language_commands`,
    `Configurations.py:79-98,176-177`): changing the language repoints the
    chat-scoped command list so the menu Telegram itself shows reads in the
    group's chosen language. This module's docstring used to list it as
    deliberately-not-reproduced, on the grounds that the command menu is
    `core_setlang`'s concern rather than a `group_configs` write — which is
    true of the *implementation* and was the right call for that port, but left
    the side effect with no owner at all: `setlang.set_group_commands` was
    written, tested and never called from here, so an admin who changed the
    language through `/config` saw every reply change and the command menu stay
    in the old one.

    Never fatal. The write already landed and the admin has been told so; a
    `setMyCommands` failure (Telegram rate limit, a bot without the rights)
    must not turn a successful save into an error message. `set_group_commands`
    already swallows and reports its own failures with `silent=True`, which is
    exactly the shape this call site needs.
    """
    await setlang.set_group_commands(bot, group_id, str(language), silent=True)


def _is_config_reply(message: Message) -> bool:
    reply_to = message.reply_to_message
    return bool(reply_to is not None and reply_to.text and _MAGIC_MARKER in reply_to.text)


def _is_config_callback(callback: CallbackQuery) -> bool:
    return parse_callback_data(callback.data or "") is not None


# ------------------------------------------------------------------- handlers


@router.message(CommandName("config"), F.chat.type.in_({"group", "supergroup"}))
async def open_config_menu(message: Message, bot: Bot) -> None:
    """`/config` (`/configurar`, `/configure` — aliased in `cb_core.textmatch`).

    v1: `configurar`, `Configurations.py:139-167`.
    """
    ctx = await context_for(bot, message)
    if not ctx.is_admin:
        mark_outcome("refused")
        await message.reply(_text_for(_DENIED_TEXT, ctx.lang))
        tutorial = get_settings().anonymous_tutorial_file_id
        if tutorial:
            await message.answer_video(tutorial)
        return

    if ctx.actor.user_id is None:
        # An anonymous admin now passes the check above (the v1 bug this fixes —
        # see module docstring), but Telegram never tells us who they really are,
        # so there is no id to open a DM with. v1's own "couldn't reach you
        # privately" branch (the `except` in `configurar`, `Configurations.py:167`)
        # is exactly the right message for this case too.
        mark_outcome("refused")
        await message.reply(_text_for(_CANNOT_DM_TEXT, ctx.lang))
        return

    keyboard = build_menu_keyboard(ctx.group_id)
    try:
        await bot.send_message(ctx.actor.user_id, menu_text(ctx.config), reply_markup=keyboard)
    except (TelegramForbiddenError, TelegramBadRequest) as exc:
        log.info("config_menu.dm_failed", user_id=ctx.actor.user_id, error=str(exc))
        mark_outcome("refused")
        await message.reply(_text_for(_CANNOT_DM_TEXT, ctx.lang))
        return

    await message.reply(_text_for(_SENT_DM_TEXT, ctx.lang))


@router.callback_query(_is_config_callback)
async def press_config_button(callback: CallbackQuery, bot: Bot) -> None:
    """A menu button press. v1: `config_variable_button`, `Configurations.py:213-240`.

    v1 never answers this callback (`COOKIEBOT.py:360-361`), leaving the client's
    loading spinner running forever — fixed here unconditionally.
    """
    parsed = parse_callback_data(callback.data or "")
    if parsed is None or callback.message is None:  # pragma: no cover - filter already checked
        await callback.answer()
        return
    letter, group_id = parsed
    field = FIELD_BY_LETTER[letter]
    await bot.send_message(callback.message.chat.id, build_prompt(field, group_id))
    await callback.answer()


async def _write_failed_text(group_id: int, exc: BaseException) -> str:
    """The failure message, in the group's language, with the reason and the id.

    The language lookup is the same read that may have just failed, so it is
    best-effort and falls back to English: a write failure reported in the wrong
    language still reports the failure, while an exception raised in here would
    replace it with silence.
    """
    lang = "en"
    # Never let the message *about* a failure fail: this is the same read that
    # may have just broken.
    with contextlib.suppress(Exception):
        lang = locales.resolve_language((await group_config.get_config(group_id)).language)
    return locales.get(
        "config_write_failed",
        lang,
        reason=error_reason_for_chat(exc),
        trace=current_trace_id() or "-",
    )


@router.message(_is_config_reply)
async def apply_config_reply(message: Message) -> None:
    """The admin's reply to a prompt. v1: `configurar_set`, `Configurations.py:169-211`."""
    reply_text = message.reply_to_message.text or "" if message.reply_to_message else ""
    group_id = extract_group_id(reply_text)
    field = find_field_by_prompt(reply_text)
    if group_id is None or field is None:  # pragma: no cover - _is_config_reply already matched
        return

    value = parse_reply_value(field, message.text or "")
    if value is None:
        mark_outcome("refused")
        await message.reply(_INVALID_INPUT_TEXT)
        return

    try:
        await apply_change(group_id, field, value)
    except Exception as exc:  # noqa: BLE001 - the admin must learn the write did not land
        # `group_config.set_config` raises on a database failure rather than
        # degrading, which is right for a write — but it means the confirmation
        # below would otherwise be a lie. v1 crashed here and said nothing at all
        # (`Configurations.py:169-211` has no error handling), so the admin walked
        # away believing the setting had changed.
        log.warning(
            "config_menu.write_failed",
            group_id=group_id,
            column=field.column,
            error=errors.render(exc),
            error_chain=errors.chain(exc),
        )
        # Not "silent" (a message is sent) and not the middleware's own "error"
        # (nothing propagates past here) — "refused" is the closest of the three
        # buckets: the write the admin asked for did not land, and they were told.
        mark_outcome("refused")
        # Nothing propagates past here, so `TelemetryMiddleware`'s `_tell_user`
        # never runs for this failure and the admin would otherwise get the one
        # thing a support request cannot use: "try again" with no reference and
        # no reason. The trace id is the same one the Errors dashboard indexes.
        await message.answer(await _write_failed_text(group_id, exc))
        return

    if field.kind == "language" and message.bot is not None:
        await apply_language_side_effect(message.bot, group_id, value)

    try:
        await message.react([ReactionTypeEmoji(emoji="👍")])
    except Exception as exc:  # noqa: BLE001 - a cosmetic reaction must not break the confirmation
        log.info("config_menu.react_failed", error=str(exc))

    await message.answer(_SUCCESS_TEXT)


__all__ = [
    "CONFIG_FIELDS",
    "FIELD_BY_LETTER",
    "ConfigField",
    "apply_change",
    "apply_language_side_effect",
    "build_callback_data",
    "build_menu_keyboard",
    "build_prompt",
    "extract_group_id",
    "find_field_by_prompt",
    "menu_text",
    "parse_callback_data",
    "parse_reply_value",
    "router",
]
