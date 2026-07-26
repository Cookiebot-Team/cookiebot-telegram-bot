"""core_groupguardian — join captcha.

v1: `captcha_message`/`solve_captcha`/`check_captcha`
(`../COOKIEBOT-Telegram-Group-Bot/Bot/GroupShield.py:231-265,313-345`), dispatched
from `../COOKIEBOT-Telegram-Group-Bot/Bot/COOKIEBOT.py:147-148` (join gate),
`:298-299` (reply-to-caption match), `:309-316` (catch-all fallback),
`:391-395` (callback buttons).

QA: `Cookiebot-QA/features/core_groupguardian.feature` -> `qa/features/core_groupguardian.feature`.
Contract: `docs/contracts/core_groupguardian.md` (read that first — it has the full
v1/v2 table, including the two real v1 defects this port deliberately fixes:
the text-reply "solve" check never actually compared the typed digits to the
generated password, and both inline buttons that were *supposed* to prove
humanity were unconditional free passes).

## What this file assumes is true of v1 (see the contract doc for the trace)

v1's real captcha is decorative: the photo shows a 4-digit password, but
`solve_captcha`'s text branch only checks `solveattempt.isnumeric() and
len(solveattempt) == 4` — never against `password` — and both the
"I'm not a Robot!" button (self-tap, zero verification) and admin-approve
button always succeed unconditionally. This port keeps the *shape* (a message
with a challenge, solvable by text reply or by tapping a button, admin
override available) but makes verification real, using the already-compiled
`cb_core.captcha` module (`make_arithmetic`, `verify`, `callback_payload`/
`parse_callback`). The admin-override button is preserved (a legitimate v1
UX element, not a defect); the self-tap free pass is not.

## Join-priority dependency (not fixable from this file — see the report)

`on_join` fires on the same `F.new_chat_members` event `welcome.on_join`
does. v1 shows a captcha **instead of** the welcome message, only for a
*self*-join (`msg['from']['id'] == msg['new_chat_participant']['id']`,
`COOKIEBOT.py:142-150`) with the gate open; every other join (invited,
gate closed) always gets `welcome_message`. This handler raises
`SkipHandler` whenever the captcha does not apply, so `welcome.on_join` can
run instead — but that only works if `groupguardian.router` is registered
**before** `welcome.router` in `handlers/__init__.py:build_router` (not owned
by this task; `docs/contracts/core_welcome.md` already flags the same
dependency from the other side).

## Scope deliberately not built here

- Anti-raid batch-join detection (`check_raid`, `GroupShield.py:118-138`) —
  a cross-cutting, global (not per-group) concern with its own in-memory
  ledger; no QA scenario covers it and it is not part of the captcha
  contract itself.
- `util_doomlist` (`check_human`/`check_cas`/`check_banlist`/
  `check_banlist_public`, run *before* the captcha gate in v1) — not yet
  ported anywhere in this codebase. When it lands, its router must also be
  registered before this one for the same join-priority reason.
- `call_admins` (`util_calladms`) — v1's "Call Admins" captcha button pings
  the admin-notification feature. Not reimplemented here; the button is
  dropped from the v2 keyboard (see contract doc).
- Media restriction while the challenge is pending — v1's `restrictChatMember`
  call in `captcha_message` still allows `can_send_messages: True` (only
  media/stickers/previews are muted), i.e. it is the *same* mute
  `core_mediarestrict` already re-architects around `group_members.joined_at`
  rather than a native Telegram restriction. Left entirely to that feature.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    User,
)

from cb_core import admins, captcha, db, events, group_config, locales
from cb_core.logging import get_logger
from cb_gateway.context import ChatContext, context_for
from cb_gateway.handlers import welcome

log = get_logger("cb.groupguardian")

router = Router(name="groupguardian")

# v1 hardcodes 5 attempts into every fresh `Captcha.txt` line
# (`GroupShield.py:263`) regardless of language or group.
MAX_ATTEMPTS = 5

# v1's wrong-answer text is hardcoded Portuguese, unconditionally, regardless of
# the group's own language (`GroupShield.py:340` — no `language=` kwarg reaches
# `send_message`'s `translate()` call). Preserved verbatim: a user-visible quirk,
# not a silent-failure bug (AGENTS.md `/migrate-feature` Phase 2 rule).
WRONG_ANSWER_TEXT = "Senha incorreta, por favor tente novamente."

# Sentinel option for the admin-override button. Never a plausible arithmetic
# sum or emoji, so it can never collide with a real answer.
_APPROVE_OPTION = "APPROVE"


# --------------------------------------------------------------------- strings


def _captcha_strings(lang: str) -> Mapping[str, str]:
    """The nested `"captcha"` object from the ported `lib.json` catalog.

    `cb_core.locales.get` only resolves flat keys; this feature's strings are a
    nested object (`title`/`limit`/`time`/`kick`/`error_kick`/`button_approve`),
    so the fallback-to-en behaviour `locales.get` gives every other feature is
    replicated by hand here.
    """
    # `locales.catalog` is declared `Mapping[str, str]` (true for every other
    # feature's flat keys), but this one entry is the nested `"captcha"` object —
    # a real mismatch between the declared and actual shape, not something this
    # module can fix (cb_core is out of scope). Cast rather than silently typing
    # the whole function `Any`, since the isinstance check below is still real.
    raw = cast(dict[str, object], locales.catalog(lang))
    value = raw.get("captcha")
    if not isinstance(value, dict):
        value = cast(dict[str, object], locales.catalog("en")).get("captcha", {})
    return cast(Mapping[str, str], value)


# --------------------------------------------------------------------- DB seam

_UPSERT_CHALLENGE = """
INSERT INTO captcha_challenges
    (group_id, user_id, nonce, kind, answer, attempts, message_id, issued_at, expires_at, solved_at)
VALUES ($1, $2, $3, $4, $5, 0, $6, $7, $8, NULL)
ON CONFLICT (group_id, user_id) DO UPDATE SET
    nonce = EXCLUDED.nonce,
    kind = EXCLUDED.kind,
    answer = EXCLUDED.answer,
    attempts = 0,
    message_id = EXCLUDED.message_id,
    issued_at = EXCLUDED.issued_at,
    expires_at = EXCLUDED.expires_at,
    solved_at = NULL
"""


async def _issue_row(
    group_id: int,
    user_id: int,
    challenge: captcha.Challenge,
    message_id: int,
    expires_at: datetime,
) -> None:
    """Re-arms the challenge on conflict — a user who left mid-challenge and
    rejoined gets a fresh nonce/answer/attempts, never a stale row (the PK is
    `(group_id, user_id)`, so a plain INSERT would fail on rejoin).

    `issued_at` is bound once here in Python, not `now()` in the DO UPDATE
    SET: Citus rejects non-IMMUTABLE functions there on a distributed table
    (same fix as `cb_core.group_config.set_config`, `0001_initial_schema.py:436-440`).
    """
    await db.execute(
        _UPSERT_CHALLENGE,
        group_id,
        user_id,
        challenge.nonce,
        challenge.kind,
        challenge.answer,
        message_id,
        datetime.now(UTC),
        expires_at,
        name="captcha_issue",
    )


async def _fetch_pending(group_id: int, user_id: int) -> Mapping[str, Any] | None:
    """Single-shard read, filtered on `group_id` (AGENTS.md §4)."""
    return await db.fetchrow(
        "SELECT nonce, kind, answer, attempts, message_id, expires_at "
        "FROM captcha_challenges WHERE group_id = $1 AND user_id = $2 AND solved_at IS NULL",
        group_id,
        user_id,
        name="captcha_lookup",
    )


async def _fetch_by_message(group_id: int, message_id: int) -> Mapping[str, Any] | None:
    """Resolve a button press back to the challenge it belongs to.

    v1 has no per-message index at all (a flat file rewritten in full); this is
    the v2 equivalent lookup, still filtered on `group_id`.
    """
    return await db.fetchrow(
        # `message_id` is in the projection even though it is also the lookup key:
        # `_succeed` reads it off the row to delete the challenge message, and
        # leaving it out made every admin approval fail with a KeyError.
        "SELECT user_id, nonce, answer, attempts, expires_at, message_id "
        "FROM captcha_challenges WHERE group_id = $1 AND message_id = $2 AND solved_at IS NULL",
        group_id,
        message_id,
        name="captcha_lookup_by_message",
    )


async def _record_wrong_attempt(group_id: int, user_id: int, attempts: int) -> None:
    await db.execute(
        "UPDATE captcha_challenges SET attempts = $3 WHERE group_id = $1 AND user_id = $2",
        group_id,
        user_id,
        attempts,
        name="captcha_record_attempt",
    )


async def _delete_challenge(group_id: int, user_id: int) -> None:
    await db.execute(
        "DELETE FROM captcha_challenges WHERE group_id = $1 AND user_id = $2",
        group_id,
        user_id,
        name="captcha_delete",
    )


# ------------------------------------------------------------------- join event


@router.message(F.new_chat_members)
async def on_join(message: Message) -> None:
    joiners = message.new_chat_members
    if not joiners:
        return
    # Same v1 quirk `welcome.py` documents: only the first joiner in a batch
    # join is ever processed.
    newcomer = joiners[0]
    bot_user = message.bot

    if bot_user is not None and newcomer.id == bot_user.id:
        raise SkipHandler("bot's own join is not a groupguardian concern")

    from_user = message.from_user
    if from_user is None or from_user.id != newcomer.id:
        # v1: `msg['from']['id'] != msg['new_chat_participant']['id']` -> always
        # `welcome_message`, captcha never considered (`COOKIEBOT.py:136-141`).
        raise SkipHandler("invited join, not a self-join — v1 never captchas these")

    ctx = await context_for(cast(Bot, message.bot), message)
    bot_is_admin = bot_user is not None and await admins.is_admin(
        bot_user, ctx.group_id, bot_user.id
    )
    if ctx.config.captcha_timeout_seconds <= 0 or not bot_is_admin:
        # v1: `elif captchatimespan > 0 and myself['username'] in listaadmins: ...
        # else: welcome_message(...)` (`COOKIEBOT.py:147-150`).
        raise SkipHandler("captcha gate closed — v1 falls through to welcome_message")

    await _issue_challenge(message, ctx, newcomer)


async def _issue_challenge(message: Message, ctx: ChatContext, newcomer: User) -> None:
    challenge = captcha.make_arithmetic()
    minutes = round(ctx.config.captcha_timeout_seconds / 60)
    strings = _captcha_strings(ctx.lang)
    # v1's caption, byte-identical (`GroupShield.py:244`); the arithmetic prompt
    # is appended because v2 has no rendered image showing a code to type.
    title = strings.get("title", "%(name)s") % {"name": newcomer.first_name or "", "time": minutes}
    text = f"{title}\n\n{challenge.prompt}"

    rows = [
        [
            InlineKeyboardButton(
                text=opt, callback_data=captcha.callback_payload(challenge.nonce, opt)
            )
        ]
        for opt in challenge.options
    ]
    rows.append(
        [
            InlineKeyboardButton(
                text=strings.get("button_approve", "Approve"),
                callback_data=captcha.callback_payload(challenge.nonce, _APPROVE_OPTION),
            )
        ]
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=rows)

    sent = await message.reply(text, reply_markup=keyboard)
    expires_at = datetime.now(UTC) + timedelta(seconds=ctx.config.captcha_timeout_seconds)
    await _issue_row(ctx.group_id, newcomer.id, challenge, sent.message_id, expires_at)
    events.recorder().record(
        ctx.group_id, "captcha", user_id=newcomer.id, outcome="issued", handler="groupguardian"
    )


# --------------------------------------------------------- text-reply solve path


async def _is_captcha_reply(message: Message) -> bool | dict[str, Any]:
    """Structural precondition for treating a plain message as a solve attempt.

    v1 reaches `solve_captcha` from the dispatcher's final `else` (`COOKIEBOT.py:
    309-316`) for *any* unmatched, non-command message from a user with a
    pending challenge — not only replies to the captcha message. Replicated
    here as "any non-command text from a user with a pending row", which is
    the real v1 behaviour, not the narrower "must be a reply" reading of the
    more specific branch at `COOKIEBOT.py:298`.
    """
    text = message.text
    if not text:
        return False
    if text.startswith("/") and len(text) > 1:
        return False
    sender = message.from_user
    if sender is None:
        return False
    # Cheap, L1-cached config check before ever touching the DB — v1's own gate
    # (`captchatimespan > 0`), and keeps this filter a no-op cost for every
    # group that has the feature off.
    config = await group_config.get_config(message.chat.id)
    if config.captcha_timeout_seconds <= 0:
        return False
    row = await _fetch_pending(message.chat.id, sender.id)
    if row is None:
        return False
    return {"captcha_row": row}


@router.message(_is_captcha_reply)
async def on_captcha_text_reply(message: Message, captcha_row: Mapping[str, Any]) -> None:
    bot = cast(Bot, message.bot)
    ctx = await context_for(bot, message)
    submitted = (message.text or "").strip()
    # v1 deletes the user's message on both the success and the wrong-answer
    # branch (`GroupShield.py:333-334,337`).
    with contextlib.suppress(Exception):
        await message.delete()
    sender = message.from_user
    assert sender is not None  # guaranteed by _is_captcha_reply
    await _resolve_attempt(
        ctx, bot, message.chat.id, message.chat.title, sender, submitted, captcha_row
    )


# ------------------------------------------------------------------ button path


@router.callback_query(F.data.startswith("cap:"))
async def on_captcha_callback(callback: CallbackQuery) -> None:
    message = callback.message
    presser = callback.from_user
    if message is None or presser is None:
        with contextlib.suppress(Exception):
            await callback.answer()
        return

    nonce, option = captcha.parse_callback(callback.data or "")
    if not nonce:
        with contextlib.suppress(Exception):
            await callback.answer()
        return

    bot = cast(Bot, callback.bot)
    ctx = await context_for(bot, callback)
    row = await _fetch_by_message(ctx.group_id, message.message_id)
    if row is None or row["nonce"] != nonce:
        # Stale button: already solved, expired-and-swept, or a replay.
        with contextlib.suppress(Exception):
            await callback.answer()
        return

    newcomer_id = row["user_id"]
    chat_id = message.chat.id
    chat_title = message.chat.title

    if presser.id == newcomer_id:
        if option == _APPROVE_OPTION:
            # The newcomer tapped the admin-only button themselves: no effect,
            # matching v1 (`CAPTCHAAPPROVE` requires `from_id` in the admin list
            # or the bot owner, `COOKIEBOT.py:391`) — the newcomer's own
            # self-tap free pass (`CAPTCHASELF`) is the fixed defect, not
            # reproduced here (see module docstring).
            with contextlib.suppress(Exception):
                await callback.answer()
            return
        await _resolve_attempt(ctx, bot, chat_id, chat_title, presser, option, row)
        with contextlib.suppress(Exception):
            await callback.answer()
        return

    if option == _APPROVE_OPTION and ctx.is_admin:
        # v1: `msg['new_chat_member'] = cookiebot.getChatMember(chat, str(user))['user']`
        # (`GroupShield.py:327`) — the approving admin isn't the newcomer, so
        # their fresh `User` has to be fetched instead of reused from the event.
        member = await bot.get_chat_member(chat_id, newcomer_id)
        await _succeed(ctx, bot, chat_id, chat_title, member.user, row)
        with contextlib.suppress(Exception):
            await callback.answer()
        return

    # Anyone else tapping any button: v1 has no matching elif branch, so
    # nothing happens. Answer anyway so Telegram clears the loading spinner.
    with contextlib.suppress(Exception):
        await callback.answer()


# --------------------------------------------------------------- shared verdicts


async def _resolve_attempt(
    ctx: ChatContext,
    bot: Bot,
    chat_id: int,
    chat_title: str | None,
    sender: User,
    submitted: str,
    row: Mapping[str, Any],
) -> None:
    if captcha.verify(row["answer"], submitted):
        await _succeed(ctx, bot, chat_id, chat_title, sender, row)
    else:
        await _fail_attempt(ctx, bot, chat_id, sender.id, row)


async def _succeed(
    ctx: ChatContext,
    bot: Bot,
    chat_id: int,
    chat_title: str | None,
    newcomer: User,
    row: Mapping[str, Any],
) -> None:
    await _delete_challenge(ctx.group_id, newcomer.id)
    events.recorder().record(
        ctx.group_id, "captcha", user_id=newcomer.id, outcome="solved", handler="groupguardian"
    )
    message_id = row["message_id"]
    if message_id is not None:
        with contextlib.suppress(Exception):
            await bot.delete_message(chat_id, message_id)
    # v1: `welcome_message(...)` (`GroupShield.py:328,335`) — reuse core_welcome's
    # own text/send logic rather than duplicating it (its module owns
    # `group_welcomes` and the placeholder-substitution contract).
    text = await welcome._welcome_text(ctx, chat_title, newcomer)  # noqa: SLF001
    await welcome._send_welcome_text(bot, chat_id, text)  # noqa: SLF001


async def _fail_attempt(
    ctx: ChatContext, bot: Bot, chat_id: int, user_id: int, row: Mapping[str, Any]
) -> None:
    attempts = row["attempts"] + 1
    if attempts >= MAX_ATTEMPTS:
        await _kick(ctx, bot, chat_id, user_id, reason_key="limit")
        return
    if row["expires_at"] <= datetime.now(UTC):
        await _kick(ctx, bot, chat_id, user_id, reason_key="time")
        return
    await _record_wrong_attempt(ctx.group_id, user_id, attempts)
    with contextlib.suppress(Exception):
        await bot.send_message(chat_id, WRONG_ANSWER_TEXT)


async def _kick(ctx: ChatContext, bot: Bot, chat_id: int, user_id: int, *, reason_key: str) -> None:
    """v1: `cookiebot.kickChatMember` + a 30s-later `unbanChatMember`
    (`GroupShield.py:298-305`) — a temporary ban, not permanent."""
    strings = _captcha_strings(ctx.lang)
    reason = strings.get(reason_key, reason_key)
    await _delete_challenge(ctx.group_id, user_id)
    try:
        await bot.ban_chat_member(chat_id, user_id)
    except TelegramAPIError as exc:
        log.warning("captcha.kick_failed", error=str(exc), reason=reason_key)
        text = strings.get("error_kick", "%(user)s") % {"user": user_id, "reason": reason}
        with contextlib.suppress(Exception):
            await bot.send_message(chat_id, text)
        events.recorder().record(
            ctx.group_id,
            "captcha",
            user_id=user_id,
            outcome=f"kick_failed_{reason_key}",
            handler="groupguardian",
        )
        return

    text = strings.get("kick", "%(user)s") % {"user": user_id, "reason": reason}
    with contextlib.suppress(Exception):
        await bot.send_message(chat_id, text)
    events.recorder().record(
        ctx.group_id,
        "captcha",
        user_id=user_id,
        outcome=f"kicked_{reason_key}",
        handler="groupguardian",
    )
    _schedule_unban(bot, chat_id, user_id)


# The event loop keeps only a weak reference to a running task, so a bare
# `create_task(...)` can be garbage-collected mid-sleep and the ban is never
# lifted. Holding the task here until it finishes is what RUF006 asks for, and
# the suppression this replaced silenced exactly the bug that rule warns about.
_pending_unbans: set[asyncio.Task[None]] = set()


def _schedule_unban(bot: Bot, chat_id: int, user_id: int) -> None:
    """v1 used a `threading.Timer` for the same 30s window (`GroupShield.py`).

    Still in-process, and therefore still lost if the gateway restarts inside
    the window — the durable version is a deferred cb-worker job, which needs
    gateway->worker enqueue wiring that does not exist yet. Recorded in
    docs/contracts/core_groupguardian.md rather than pretended away.
    """
    task = asyncio.create_task(_delayed_unban(bot, chat_id, user_id))
    _pending_unbans.add(task)
    task.add_done_callback(_pending_unbans.discard)


async def _delayed_unban(bot: Bot, chat_id: int, user_id: int) -> None:
    await asyncio.sleep(30)
    with contextlib.suppress(Exception):
        await bot.unban_chat_member(chat_id, user_id)
