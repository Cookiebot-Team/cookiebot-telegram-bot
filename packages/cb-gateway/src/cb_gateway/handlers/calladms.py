"""util_calladms — `/adm` (aliased `/admin`, `/report`) summons the group's admins.

v1: `UserRegisters.py:168-176` `call_admins_ask` (the confirmation prompt),
`UserRegisters.py:178-203` `call_admins` (the actual ping + DM fan-out), dispatched
at `COOKIEBOT.py:274-275` (`/adm`, `@admin`, `@adm`, `/report` -> `call_admins_ask`,
un-gated by any admin check or the `functionsUtility` flag) and the `ADM` callback
branch at `COOKIEBOT.py:396-408`. Aliases `adm`/`admin`/`report` -> `calladms`
already live in `cb_core.textmatch.COMMAND_ALIASES`.

QA: `Cookiebot-QA/features/util_calladms.feature` -> `qa/features/util_calladms.feature`.
Contract: `docs/contracts/util_calladms.md` (full v1/v2 table and the fan-out
decision below).

All four v1 triggers work. The slash forms (`/adm`, `/admin`, `/report`) come
through `COMMAND_ALIASES`; the bare-word forms (`@admin`, `@adm`,
`COOKIEBOT.py:274`) are matched by `_MENTION_TRIGGER` below, because
`parse_command` only ever inspects `/`-prefixed text and teaching it otherwise
would turn every mention of a user called "admin" into a command. The one
narrowing: v1's raw `startswith` also fired on `@admins` and `@adminfoo`, and the
word boundary here does not — recorded in the contract.

**The DM fan-out is a `cb-worker` job, not reply-path work.** v1's
`call_admins` (`UserRegisters.py:178-203`) DMs every admin individually — a
distinct Telegram chat per admin, throttled with `time.sleep(0.1)` — which is
multi-chat fan-out exactly as AGENTS.md section 2.4 describes it. Once the
group ping is sent, `confirm_call_admins` below enqueues
`jobs.CALLADMS_NOTIFY_ADMINS` (scalars only, same discipline
`handlers/everyone.py` already established for its own fan-out) and returns;
no DM, no per-admin Telegram call happens here. The job itself is
`cb_worker/jobs/calladms.py`; see `.specs/features/util_calladms/` and
`docs/contracts/util_calladms.md` for the full DM-half behaviour contract.

Everything else is exact: the confirmation prompt (open to anyone — v1 never
checks who may *ask* to call admins), the 600-second staleness window measured
from the prompt's own timestamp, the unconditional delete of the prompt on any
button press, and the group-ping text (admins mentioned by username, including
the bot's own username when the bot itself is an admin — v1 only excludes the
bot from the *DM* loop, never from the mention text, and the DM-fan-out job
preserves that same exclusion).
"""

from __future__ import annotations

import contextlib
import re
from datetime import UTC, datetime

from aiogram import Bot, F, Router
from aiogram.types import (
    CallbackQuery,
    InaccessibleMessage,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from cb_core import jobs
from cb_core.logging import get_logger
from cb_gateway.context import context_for, t
from cb_gateway.filters import CommandName
from cb_gateway.queue import enqueue
from cb_gateway.telemetry import mark_outcome

log = get_logger("cb.calladms")

router = Router(name="calladms")

# Hardcoded, English-only in v1 (`COOKIEBOT.py:402`) — handed straight to
# `answerCallbackQuery`, never routed through `i18n.get`. Preserved verbatim: it
# never localises in v1 either, regardless of the group's language.
TOO_OLD_TEXT = "Message too old, use /adm again"

# v1's staleness window (`COOKIEBOT.py:401`): more than 600 seconds since the
# *confirmation prompt* was sent (not the original `/adm` message).
STALE_AFTER_SECONDS = 600

_YES_TOKEN = "CALLADMS YES"
_NO_TOKEN = "CALLADMS NO"


# --------------------------------------------------------------- callback wire


def build_callback_data(confirmed: bool, original_message_id: int) -> str:
    """`"CALLADMS YES|NO {message_id}"` — v2's own wire shape.

    v1's equivalent embeds the language too (`f"ADM Yes {language} {msg['message_id']}"`,
    `UserRegisters.py:173-174`); not needed here since `context_for` re-derives the
    group's language from the callback's own chat at press time.
    """
    token = _YES_TOKEN if confirmed else _NO_TOKEN
    return f"{token} {original_message_id}"


def parse_callback_data(data: str) -> tuple[bool, int] | None:
    """The inverse of `build_callback_data`; `None` for anything malformed or unrelated."""
    parts = data.split()
    if len(parts) != 3 or parts[0] != "CALLADMS" or parts[1] not in {"YES", "NO"}:
        return None
    try:
        original_message_id = int(parts[2])
    except ValueError:
        return None
    return parts[1] == "YES", original_message_id


def _is_calladms_callback(callback: CallbackQuery) -> bool:
    return parse_callback_data(callback.data or "") is not None


def is_stale(prompt_date: datetime, *, now: datetime | None = None) -> bool:
    """v1: `(datetime.now(utc).timestamp() - msg['message']['date']) > 600` (`COOKIEBOT.py:401`)."""
    reference = now if now is not None else datetime.now(UTC)
    return (reference - prompt_date).total_seconds() > STALE_AFTER_SECONDS


# ------------------------------------------------------------------- admin fetch


async def admin_usernames(bot: Bot, group_id: int) -> list[str]:
    """Usernames to `@mention`, fetched like v1's `get_admins` (`Configurations.py:56-77`).

    Calls Telegram directly rather than going through `cb_core.admins`: that
    module's `Admin` dataclass (`docs/contracts/admins.md`) deliberately carries
    only `user_id`/`role`/privilege flags, never a username — nothing in M1
    before this port needed one — so it cannot answer "what do I mention this
    admin as" without a second Telegram round trip per admin, which is worse
    than the one call here. See `docs/contracts/util_calladms.md` policy #3.

    A Telegram failure degrades to "mention nobody" (logged) rather than
    propagating: v1's own `get_admins` has no failure handling at all and would
    silently drop the entire update; failing softer costs nothing here, since
    this only runs after a human has already pressed "confirm".
    """
    try:
        raw_admins = await bot.get_chat_administrators(group_id)
    except Exception as exc:  # noqa: BLE001 - Telegram is the outside world; degrade, don't crash
        log.warning("calladms.admin_fetch_failed", group_id=group_id, error=str(exc))
        return []
    return [admin.user.username for admin in raw_admins if admin.user.username]


# ------------------------------------------------------------------- handlers


# v1 dispatches this on four prefixes, not two:
#   msg['text'].startswith(("/adm", "@admin", "@adm", "/report"))   COOKIEBOT.py:274
# The slash forms arrive through COMMAND_ALIASES; the two `@` forms cannot,
# because `parse_command` only ever looks at `/`-prefixed text. Matching them
# here keeps the parser's contract intact — teaching it to treat `@admin` as a
# command would make every mention of a user called "admin" a command — while
# still honouring AGENTS.md §2.1: no v1 trigger stops working.
_MENTION_TRIGGER = re.compile(r"^@adm(in)?\b", re.IGNORECASE)


def _is_mention_trigger(message: Message) -> bool:
    return bool(message.text and _MENTION_TRIGGER.match(message.text))


@router.message(_is_mention_trigger, F.chat.type.in_({"group", "supergroup"}))
@router.message(CommandName("calladms"), F.chat.type.in_({"group", "supergroup"}))
async def ask_call_admins(message: Message, bot: Bot) -> None:
    """`/adm`, `/admin`, `/report` (aliased). v1: `call_admins_ask`, `UserRegisters.py:168-176`.

    Open to anyone: v1 never gates who may *ask* to call admins, only what
    happens once someone confirms.
    """
    ctx = await context_for(bot, message)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✔️", callback_data=build_callback_data(True, message.message_id)
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌", callback_data=build_callback_data(False, message.message_id)
                )
            ],
        ]
    )
    await message.reply(t(ctx, "call_admin_ask"), reply_markup=keyboard)


@router.callback_query(_is_calladms_callback)
async def confirm_call_admins(callback: CallbackQuery, bot: Bot) -> None:
    """The Yes/No press. v1: the `ADM` callback branch, `COOKIEBOT.py:396-408`.

    No check on who may press this either — v1 places none, and there is no
    caller identity encoded in the callback data to check against.
    """
    if callback.message is None:  # pragma: no cover - filter already matched the data shape
        await callback.answer()
        return
    parsed = parse_callback_data(callback.data or "")
    if parsed is None:  # pragma: no cover - filter already checked
        await callback.answer()
        return
    confirmed, original_message_id = parsed
    chat_id = callback.message.chat.id

    # v1 deletes the prompt unconditionally, before checking staleness
    # (`COOKIEBOT.py:400`).
    with contextlib.suppress(Exception):
        await bot.delete_message(chat_id, callback.message.message_id)

    if isinstance(callback.message, InaccessibleMessage):
        # Telegram itself no longer knows this message's real timestamp (it
        # reports `date=0` for one it can no longer serve) — no v1 equivalent,
        # since telepot never surfaced this case; treating it as stale is the
        # closest honest answer and reuses the same user-facing text.
        mark_outcome("refused")
        await callback.answer(text=TOO_OLD_TEXT)
        return

    if is_stale(callback.message.date):
        mark_outcome("refused")
        await callback.answer(text=TOO_OLD_TEXT)
        return

    # v1 never answers this callback on the confirmed/cancelled branches
    # (`COOKIEBOT.py:404-408`), leaving the presser's client spinner running
    # forever. Fixed here unconditionally, same call `config_menu.py` already
    # makes for the equivalent v1 bug (`docs/contracts/util_config.md`).
    await callback.answer()

    ctx = await context_for(bot, callback)

    if not confirmed:
        await bot.send_message(chat_id, t(ctx, "canceled"))
        return

    presser = callback.from_user
    caller = (presser.username or presser.first_name or "") if presser is not None else ""
    usernames = await admin_usernames(bot, chat_id)
    mentions = " ".join(f"@{username}" for username in usernames)
    text = mentions + t(ctx, "call_admin", caller=caller)
    await bot.send_message(chat_id, text)

    # The DM fan-out (`UserRegisters.py:190-203`) is cb-worker's job, not the
    # reply path's (AGENTS.md §2.4, module docstring). Scalars only, same
    # discipline `handlers/everyone.py` uses for its own fan-out — the worker
    # re-resolves admins itself (design R2.2) rather than trusting the
    # username list this handler already fetched for the group ping.
    await enqueue(
        jobs.CALLADMS_NOTIFY_ADMINS,
        group_id=chat_id,
        chat_title=callback.message.chat.title or "",
        original_message_id=original_message_id,
        lang=ctx.lang,
    )


__all__ = [
    "STALE_AFTER_SECONDS",
    "TOO_OLD_TEXT",
    "admin_usernames",
    "build_callback_data",
    "confirm_call_admins",
    "is_stale",
    "parse_callback_data",
    "router",
]
