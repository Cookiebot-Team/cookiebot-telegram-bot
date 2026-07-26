"""core_rules — `/rules` displays the group's rules; `/newrules` lets an admin set them.

v1:
  - display: `rules_message`, `../COOKIEBOT-Telegram-Group-Bot/Bot/GroupShield.py:49-63`
    (calls `substitute_user_tags`, `GroupShield.py:38-47`).
  - prompt:  `new_rules_message`, `Configurations.py:281-283`.
  - capture: `update_rules_message`, `Configurations.py:269-279`.
  - dispatch: `COOKIEBOT.py:266-269` (`/newrules`,`/novasregras`,`/nuevasreglas` and
    `/rules`,`/regras`,`/reglas`; aliases already in `cb_core/textmatch.py`), and
    the reply-capture `elif` at `COOKIEBOT.py:293-295`.

QA: `Cookiebot-QA/features/core_rules.feature` -> `qa/features/core_rules.feature`.
Contract: `docs/contracts/core_rules.md` (read that first for the full v1/v2 table
and the one QA scenario that does not match v1's real behaviour).

How `/newrules` captures the new text (v1, exactly, `Configurations.py:281-283` +
`COOKIEBOT.py:293-295`): NOT an argument, NOT a single command — a two-step
conversation. `/newrules` always replies with a fixed, hardcoded English prompt,
*regardless of who ran it* (no admin gate on the command itself). Whoever later
replies to that literal prompt text is treated as the submission; only there is
admin checked, and only there does a rejection happen. v1 also requires the
reply's own text not itself look like a command (`COOKIEBOT.py:186` gates the
whole command-dispatch `if`, so the reply-capture `elif` at line 293 is only
reachable when the incoming text does not start with `/`) — replicated in
`_is_new_rules_reply` below.

Persistence: `group_rules` (`packages/cb-api/migrations/versions/0001_initial_schema.py`),
one row per group (`PRIMARY KEY (group_id)`), distributed on `group_id`, colocated
with `groups`. v1's REST layer did PUT-then-POST-on-404
(`Configurations.py:274-276`); the single-shard equivalent here is an upsert.
"""

from __future__ import annotations

import contextlib
from typing import cast

from aiogram import Bot, Router
from aiogram.types import Message

from cb_core import db
from cb_gateway.context import context_for, t
from cb_gateway.filters import CommandName
from cb_gateway.telemetry import mark_outcome

router = Router(name="rules")

# Hardcoded verbatim in v1 (Configurations.py:283) — never localised, unlike
# almost every other user-facing string. Preserved byte-for-byte: it is also the
# literal string v1 matches on when looking for the reply that sets new rules.
NEW_RULES_PROMPT = (
    "If you are an admin, REPLY THIS MESSAGE with the message that will be "
    "displayed when someone asks for the rules"
)
# Also hardcoded English-only in v1 (Configurations.py:271,278) — same quirk.
NOT_ADMIN_TEXT = "You are not a group admin!"
RULES_UPDATED_TEXT = "Updated rules message! ✅"

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


def _substitute_user_tags(text: str, message: Message) -> str:
    """v1's `substitute_user_tags` (`GroupShield.py:38-47`).

    Replaces any of v1's usertag spellings with the *requester's* own name (the
    person who ran `/rules`, not whoever wrote the rules) — `@username` if they
    have one, else their first name.
    """
    user = message.from_user
    if user is None:
        return text
    replacement = f"@{user.username}" if user.username else (user.first_name or "")
    for tag in _USER_TAGS:
        if tag in text:
            text = text.replace(tag, replacement)
    return text


def _is_new_rules_reply(message: Message) -> bool:
    """Structural precondition for capturing a `/newrules` reply, ported exactly.

    v1's whole command-dispatch chain lives inside `if text.startswith("/") and
    len(text) > 1`, and the reply-capture branch is a sibling `elif` of that `if`
    (`COOKIEBOT.py:186,293`) — so it is only reached when the incoming text does
    *not* itself look like a command.
    """
    text = message.text
    if text is None:
        return False
    if text.startswith("/") and len(text) > 1:
        return False
    reply = message.reply_to_message
    return reply is not None and reply.text == NEW_RULES_PROMPT


# --------------------------------------------------------------------- DB seam


async def _fetch_rules(group_id: int) -> str | None:
    """Single-shard read, filtered on `group_id` (AGENTS.md §4)."""
    row = await db.fetchrow(
        "SELECT body FROM group_rules WHERE group_id = $1", group_id, name="rules_lookup"
    )
    return row["body"] if row is not None else None


async def _upsert_rules(group_id: int, user_id: int | None, body: str) -> None:
    """v1's PUT-then-POST-on-404 (`Configurations.py:274-276`), as one upsert."""
    await db.execute(
        """
        INSERT INTO group_rules (group_id, body, updated_by, updated_at)
        VALUES ($1, $2, $3, now())
        ON CONFLICT (group_id) DO UPDATE
        SET body = EXCLUDED.body,
            updated_by = EXCLUDED.updated_by,
            updated_at = EXCLUDED.updated_at
        """,
        group_id,
        body,
        user_id,
        name="rules_upsert",
    )


# ------------------------------------------------------------------- handlers


@router.message(CommandName("rules"))
async def rules(message: Message) -> None:
    ctx = await context_for(cast(Bot, message.bot), message)
    body = await _fetch_rules(ctx.group_id)
    if body is None:
        await message.reply(t(ctx, "no_rules"))
        return

    text = body.replace("\\n", "\n")
    text = _substitute_user_tags(text, message)
    if not text:
        # v1: `if not len(regras): return` (GroupShield.py:58-59) — silence, not
        # the empty-state message; a row exists, it is just blank after
        # substitution.
        mark_outcome("silent")
        return
    if not text.endswith("@MekhyW"):
        text += t(ctx, "questions")
    await message.reply(text)


@router.message(CommandName("newrules"))
async def new_rules(message: Message) -> None:
    # No AdminOnly() filter here on purpose: v1 shows this prompt to anyone who
    # runs /newrules and only checks admin status on the reply that follows.
    await message.reply(NEW_RULES_PROMPT)


@router.message(_is_new_rules_reply)
async def capture_new_rules(message: Message) -> None:
    bot = cast(Bot, message.bot)
    ctx = await context_for(bot, message)
    if not ctx.is_admin:
        mark_outcome("refused")
        await message.reply(NOT_ADMIN_TEXT)
        return

    text = message.text or ""
    await _upsert_rules(ctx.group_id, ctx.actor.user_id, text)
    await message.reply(RULES_UPDATED_TEXT)

    prompt = message.reply_to_message
    if prompt is not None:
        # Best-effort, like v1's `delete_message` (`universal_funcs.py:340-344`),
        # which swallows the exception rather than letting a missing/already
        # deleted prompt fail the whole confirmation.
        with contextlib.suppress(Exception):
            await bot.delete_message(message.chat.id, prompt.message_id)
