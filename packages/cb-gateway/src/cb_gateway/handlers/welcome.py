"""core_welcome — join greeting + `/newwelcome`.

v1:
  - join greeting: `welcome_message`/`substitute_user_tags`,
    `../COOKIEBOT-Telegram-Group-Bot/Bot/GroupShield.py:38-171`.
  - prompt: `new_welcome_message`, `Configurations.py:265-267`.
  - capture: `update_welcome_message`, `Configurations.py:253-263`.
  - dispatch: `COOKIEBOT.py:121-150` (join event), `:264-265` (`/newwelcome`,
    `/novobemvindo`, `/nuevabienvenida`; the first two aliases already in
    `cb_core/textmatch.py`), and the reply-capture `elif` at `:290-292`.

QA: `Cookiebot-QA/features/core_welcome.feature` -> `qa/features/core_welcome.feature`.
Contract: `docs/contracts/core_welcome.md` (read that first for the full v1/v2
table, the complete placeholder table, and the one QA scenario that does not
match v1's real behaviour).

How `/newwelcome` captures the new text (v1, exactly — the same two-step shape
as `/newrules`, see `rules.py`): `/newwelcome` always replies with a fixed,
hardcoded English prompt, *regardless of who ran it* (no admin gate on the
command itself). Whoever later replies to that literal prompt text is treated
as the submission; only there is admin checked, and only there does a
rejection happen. A reply that itself looks like a command is not captured
(`COOKIEBOT.py:186,290`) — replicated in `_is_welcome_reply` below.

Two things v1 does in the same `welcome_message` function are deliberately
**not** here (full reasoning in the contract doc):

- The `limbotimespan` media restriction (`restrictChatMember` + `restrict_message`)
  belongs to `core_mediarestrict`, which v2's schema already re-architects
  around `group_members.joined_at` rather than a native Telegram mute at join
  time.
- The pixel-art welcome-card image (OpenCV compositing) is out of scope on the
  gateway's synchronous reply path (AGENTS.md §4); this port always takes v1's
  own fallback branch — a plain, non-reply text message — which is what a real
  v1 deployment sends whenever the card fails to render.

Persistence: `group_welcomes` (`packages/cb-api/migrations/versions/0001_initial_schema.py`),
one row per group (`PRIMARY KEY (group_id)`), distributed on `group_id`,
colocated with `groups`. v1's REST layer did PUT-then-POST-on-404
(`Configurations.py:258-260`); the single-shard equivalent here is an upsert.
"""

from __future__ import annotations

import contextlib
import html
from typing import cast

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message, ReactionTypeEmoji, User

from cb_core import audit, group_texts, locales
from cb_core.logging import get_logger
from cb_gateway.context import ChatContext, context_for, t
from cb_gateway.filters import CommandName

log = get_logger("cb.welcome")

router = Router(name="welcome")

# Hardcoded verbatim in v1 (Configurations.py:267) — never localised, unlike
# almost every other user-facing string. Preserved byte-for-byte: it is also
# the literal string v1 matches on when looking for the reply that sets the
# new welcome message.
WELCOME_PROMPT = (
    "If you are an admin, REPLY THIS MESSAGE with the message that will be "
    "displayed when someone joins the group.\n\n"
    "You can include <user> to be replaced with the user name"
)
#: What actually goes on the wire. This bot sends `parse_mode=HTML`, and
#: `<user>` is not a tag Telegram knows, so sending the prompt unescaped fails
#: the whole command with:
#:
#:     Bad Request: can't parse entities: Unsupported start tag "user" at byte
#:     offset 127
#:
#: — which is exactly what `/newwelcome` did in UAT, every time, for every
#: caller. v1 sent with no parse_mode at all (`Configurations.py:267`), so the
#: string never had to survive an entity parser.
#:
#: Escaped rather than reworded, and escaped *here* rather than in the constant,
#: because `WELCOME_PROMPT` is also what `_is_welcome_reply` matches an incoming
#: reply's `reply_to_message.text` against — and Telegram hands that back
#: already rendered, i.e. with `<user>` in it. The constant has to stay the text
#: as the user sees it; only the wire form is escaped.
WELCOME_PROMPT_HTML = html.escape(WELCOME_PROMPT, quote=False)
# Also hardcoded English-only in v1 (Configurations.py:255) — same quirk.
NOT_ADMIN_TEXT = "You are not a group admin!"
WELCOME_UPDATED_TEXT = "Welcome message updated! ✅"

# GroupShield.py:40 — every one of these resolves identically; see the
# placeholder table in docs/contracts/core_welcome.md.
_USER_TAGS = (
    "{user}",
    "{username}",
    "{mention}",
    "$user",
    "$username",
    "$(user)",
    "$(username)",
    "<user>",
    "<username>",
    "<name>",
)


# --------------------------------------------------------------------- pure logic


def _substitute_user_tags(text: str, user: User) -> str:
    """GroupShield.py:38-47, verbatim: `@username` if set, else `first_name`.

    Unlike `rules.py`'s version of this same v1 function (which substitutes the
    *requester's* name), the welcome path always substitutes the *new joiner*'s
    name (`msg['new_chat_member']`, GroupShield.py:39,66,144) — the two callers
    use different halves of v1's `user = msg['new_chat_member'] if ... else
    msg['from']` fallback.

    Every placeholder in `_USER_TAGS` expands to the exact same value — there
    is no per-tag distinction in v1. Any other placeholder (`{chat}`, `%s`,
    ...) is left completely unchanged: `str.replace` only ever touches these
    ten literal substrings, over every occurrence, not just the first.
    """
    replacement = f"@{user.username}" if user.username else (user.first_name or "")
    for tag in _USER_TAGS:
        if tag in text:
            text = text.replace(tag, replacement)
    return text


def _render_custom_welcome(stored_body: str, newcomer: User) -> str:
    """GroupShield.py:160-161: unescape literal `\\n`, then substitute tags.

    The unescape runs unconditionally, even when no placeholder is present —
    it is v1's fix for a welcome message that arrived as JSON-escaped text.
    """
    unescaped = stored_body.replace("\\n", "\n")
    return _substitute_user_tags(unescaped, newcomer)


def _default_welcome(lang: str, chat_title: str | None) -> str:
    """GroupShield.py:154-158: `welcome_user` when a chat title is known
    (always true for a real group), else the generic `welcome`."""
    if chat_title:
        return locales.get("welcome_user", lang, user=chat_title)
    return locales.get("welcome", lang)


def _sanitize_for_plain_retry(text: str) -> str:
    """universal_funcs.py:210,220: v1's crude recovery from a Telegram HTML
    parse-entity error — strip every backslash and every `>`, then resend with
    no `parse_mode` (plain text, no entity parsing) rather than crashing."""
    return text.replace("\\", "").replace(">", "")


def _is_welcome_reply(message: Message) -> bool:
    """Structural precondition for capturing a `/newwelcome` reply, ported
    exactly from `rules.py`'s `_is_new_rules_reply` (same v1 shape).

    v1's whole command-dispatch chain lives inside `if text.startswith("/")
    and len(text) > 1`, and the reply-capture branch is a sibling `elif` of
    that `if` (`COOKIEBOT.py:186,290`) — so it is only reached when the
    incoming text does not itself look like a command. A lone `"/"` (length 1)
    does *not* count as a command in v1's guard, so it still falls through to
    reply-capture.
    """
    text = message.text
    if text is None:
        return False
    if text.startswith("/") and len(text) > 1:
        return False
    reply = message.reply_to_message
    return reply is not None and reply.text == WELCOME_PROMPT


# ----------------------------------------------------------------------- db i/o


async def _fetch_welcome_body(group_id: int) -> str | None:
    """The stored body, or `None`. The SQL lives in `cb_core.group_texts`, shared
    with the Mini App's config API (AGENTS.md §8); this wrapper is the seam the
    unit tests monkeypatch."""
    record = await group_texts.get_welcome(group_id)
    return record.body if record is not None else None


async def _save_welcome(group_id: int, user_id: int | None, body: str) -> None:
    """v1's PUT-then-POST-on-404 (`Configurations.py:258-260`), as one upsert."""
    await group_texts.set_welcome(group_id, body, updated_by=user_id)


async def _welcome_text(ctx: ChatContext, chat_title: str | None, newcomer: User) -> str:
    body = await _fetch_welcome_body(ctx.group_id)
    if not body:
        # Covers both "no row at all" (v1's Not Found) and a defensively-handled
        # empty string (v1 crashes on this instead — docs/contracts/core_welcome.md).
        return _default_welcome(ctx.lang, chat_title)
    return _render_custom_welcome(body, newcomer)


async def _send_welcome_text(bot: Bot, chat_id: int, text: str) -> None:
    """v1's real, always-exercised path: a plain non-reply send, with the same
    parse-error retry v1's `send_message` performs (universal_funcs.py:207-210)."""
    try:
        await bot.send_message(chat_id, text)
    except TelegramBadRequest as exc:
        log.warning("welcome.parse_failed", error=str(exc))
        try:
            await bot.send_message(chat_id, _sanitize_for_plain_retry(text), parse_mode=None)
        except TelegramBadRequest as retry_exc:
            # v1's outer catch-all swallows this too: the group gets silence,
            # only the bot owner would have been mailed a traceback.
            log.warning("welcome.retry_failed", error=str(retry_exc))


# --------------------------------------------------------------------- join event


@router.message(F.new_chat_members)
async def on_join(message: Message) -> None:
    joiners = message.new_chat_members
    if not joiners:
        return
    # v1 quirk, preserved: Telegram still sends the deprecated singular
    # new_chat_participant/new_chat_member fields (== new_chat_members[0])
    # alongside the array, and every piece of v1's join code reads only that
    # singular field. Only the first joiner in a batch join is ever handled —
    # see docs/contracts/core_welcome.md.
    newcomer = joiners[0]
    bot = cast(Bot, message.bot)
    if newcomer.id == bot.id:
        # The bot itself joining is a separate bot-onboarding concern
        # (COOKIEBOT.py:122-135), not core_welcome.
        return

    ctx = await context_for(bot, message)
    if newcomer.is_bot:
        await message.reply(t(ctx, "new_bot_participant"))
        return

    text = await _welcome_text(ctx, message.chat.title, newcomer)
    await _send_welcome_text(bot, message.chat.id, text)


# ------------------------------------------------------------------ /newwelcome


@router.message(CommandName("newwelcome"))
async def newwelcome(message: Message) -> None:
    """No admin gate here — matches v1's actual runtime behaviour exactly.

    v1's `new_welcome_message` (Configurations.py:265-267) has no permission
    check at all; the admin check only happens on the reply that follows. See
    the "QA vs. v1 conflict" section of docs/contracts/core_welcome.md for why
    this diverges from the copied QA scenario's wording.
    """
    await message.reply(WELCOME_PROMPT_HTML)


@router.message(_is_welcome_reply)
async def capture_new_welcome(message: Message) -> None:
    bot = cast(Bot, message.bot)
    ctx = await context_for(bot, message)
    if not ctx.is_admin:
        await message.reply(NOT_ADMIN_TEXT)
        return

    text = message.text or ""
    # Best-effort, like the rules handler: the `before` value is worth having
    # and never worth failing the write for.
    previous: str | None = None
    with contextlib.suppress(Exception):
        previous = await _fetch_welcome_body(ctx.group_id)
    await _save_welcome(ctx.group_id, ctx.actor.user_id, text)
    # Same trail the Mini App writes to (`cb_api.routers.groups`).
    await audit.record(
        ctx.group_id,
        audit.WELCOME_UPDATED,
        actor_user_id=ctx.actor.user_id,
        surface="telegram",
        summary="welcome message updated",
        before={"body": previous},
        after={"body": text},
    )

    # Cosmetic side effect — best-effort, matching v1's exposure where a
    # failure here is silently swallowed by the outer handler and never blocks
    # the confirmation the user actually cares about (Configurations.py:261,
    # universal_funcs.py:300-305 has no try/except of its own either).
    with contextlib.suppress(Exception):
        await message.react(reaction=[ReactionTypeEmoji(emoji="\U0001f44d")], is_big=True)

    await message.reply(WELCOME_UPDATED_TEXT)

    prompt = message.reply_to_message
    if prompt is not None:
        # Best-effort, like v1's `delete_message` (`universal_funcs.py:340-344`),
        # which swallows the exception rather than letting a missing/already
        # deleted prompt fail the whole confirmation.
        with contextlib.suppress(Exception):
            await bot.delete_message(message.chat.id, prompt.message_id)
