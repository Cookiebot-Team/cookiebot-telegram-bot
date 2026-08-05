"""util_postforwarder — `/divulgar`, the approval workflow, `/repost`, the relay.

v1: `../COOKIEBOT-Telegram-Group-Bot/Bot/Publisher.py` —
`ask_publisher_command` (`:57-75`), `ask_approval` (`:77-92`), `deny_post`
(`:223-228`), `schedule_autopost` (`:288-314`), `check_notify_post_reply`
(`:359-369`); dispatched at `COOKIEBOT.py:205,208,303,370-375`. The two slow
halves — rendering an approved post into the Mural and fanning it out, and
delivering the scheduled forwards — are `cb_worker/jobs/publisher.py`.

Spec: `.specs/features/util_postforwarder/`. Contract:
`docs/contracts/util_postforwarder.md`.

Three things worth knowing before editing this file:

1. **The approve button is authorised by *chat*, not by user.** v1 checked
   nothing at all (`COOKIEBOT.py:372-373`) and relied on the buttons only ever
   appearing in a private approval chat — but a callback payload is a plain
   string, and anyone who learns its shape can replay it from anywhere. D-PF-2:
   `yPub`/`nPub` are accepted only from `settings.approval_chat_id`.
   `SendToApprovalPub` keeps v1's openness, because that one is the ✔️ on the
   group's own prompt and group members are meant to press it.

2. **The reply relay is a separate router** (`relay_router`), registered at v1's
   own position in the `elif` chain — after the captcha-reply and complaint-reply
   checks, before the conversational AI. Fold it into `router` below and a reply
   to a published post gets answered by the AI instead.

3. **The publisher is inert until a deployment configures it.** v1 hardcoded one
   deployment's Mural and approval channels (`:20-22`); with those unset there is
   nowhere to render into, so both commands answer `publisher_unavailable`
   rather than half-running.
"""

from __future__ import annotations

import contextlib
import random
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.enums import ChatType
from aiogram.types import (
    CallbackQuery,
    InaccessibleMessage,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReactionTypeEmoji,
)
from whenever import Instant

from cb_core import jobs, pending_posts, publisher, scheduled_posts
from cb_core.logging import get_logger
from cb_core.settings import get_settings
from cb_gateway.context import ChatContext, context_for, t
from cb_gateway.filters import CommandName
from cb_gateway.queue import enqueue

log = get_logger("cb.publisher")

router = Router(name="publisher")
relay_router = Router(name="publisher_relay")

#: English-only in v1 (`:84`), handed straight to `sendMessage` in the approval
#: chat and never routed through `i18n.get`. That chat is one operator chat with
#: one language, so it stays a constant rather than becoming a locale key.
APPROVE_PROMPT = "Approve post?"

#: v1's four approve buttons (`:86-89`): label, days, NSFW flag.
APPROVAL_OPTIONS: tuple[tuple[str, int, bool], ...] = (
    ("✔️ 7 days (NSFW)", 7, True),
    ("✔️ 7 days", 7, False),
    ("✔️ 3 days", 3, False),
    ("✔️ 1 day", 1, False),
)

#: `schedule_autopost`'s "no limit" (`:306`). Stored as-is so `/deleteposts`
#: and the delivery sweep treat it like any other countdown, which is what v1
#: does — there is no "forever" flag, only a large number.
REPOST_UNLIMITED_DAYS = 9999

_SUBMIT_TOKEN = "SendToApprovalPub"
_APPROVE_TOKEN = "yPub"
_DENY_TOKEN = "nPub"


# --------------------------------------------------------------- the callback wire


def build_approval_request(
    *, origin_chat_id: int, chat_id: int, forward_from_message_id: int, message_id: int
) -> InlineKeyboardMarkup:
    """The ✔️/❌ prompt v1 attaches to a submitted post (`:50-54`).

    Exported for `util_postgetter`, which shows the same prompt when Telegram
    auto-forwards a linked channel's post. The ❌ payload is the bare token with
    no message id — v1's, and `deny_post` returns early on a one-field payload
    (`:224-225`), so that button deletes the prompt and nothing else. Preserved
    rather than "improved" into a cache eviction v1 never performed.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✔️",
                    callback_data=(
                        f"{_SUBMIT_TOKEN} {origin_chat_id} {chat_id} "
                        f"{forward_from_message_id} {message_id}"
                    ),
                )
            ],
            [InlineKeyboardButton(text="❌", callback_data=_DENY_TOKEN)],
        ]
    )


def _build_approval_keyboard(
    *,
    origin_chat_id: int,
    requester_chat_id: int,
    forward_from_message_id: int,
    requester_user_id: int,
    requester_message_id: int,
) -> InlineKeyboardMarkup:
    """v1's five approval buttons (`:85-91`), same payload field order."""
    rows = [
        [
            InlineKeyboardButton(
                text=label,
                callback_data=(
                    f"{_APPROVE_TOKEN} {origin_chat_id} {requester_chat_id} "
                    f"{forward_from_message_id} {requester_user_id} {days} "
                    f"{requester_message_id} {1 if nsfw else 0}"
                ),
            )
        ]
        for label, days, nsfw in APPROVAL_OPTIONS
    ]
    rows.append(
        [InlineKeyboardButton(text="❌", callback_data=f"{_DENY_TOKEN} {forward_from_message_id}")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def parse_submit(data: str) -> tuple[int, int, int, int] | None:
    """`SendToApprovalPub {origin} {chat} {fwd_msg} {msg}` -> the four ids."""
    parts = data.split()
    if len(parts) != 5 or parts[0] != _SUBMIT_TOKEN:
        return None
    try:
        return int(parts[1]), int(parts[2]), int(parts[3]), int(parts[4])
    except ValueError:
        return None


def parse_approve(data: str) -> tuple[int, int, int, int, int, int, bool] | None:
    """`yPub {origin} {chat} {fwd_msg} {user} {days} {msg} {nsfw}`.

    v1 reads eight fields with `split()[:8]` (`:231`) and would happily accept a
    longer payload; this requires exactly eight, since nothing v2 emits has more
    and a longer one is a replay attempt rather than a press.
    """
    parts = data.split()
    if len(parts) != 8 or parts[0] != _APPROVE_TOKEN:
        return None
    try:
        origin, chat, fwd_msg, user, days, msg = (int(p) for p in parts[1:7])
    except ValueError:
        return None
    if parts[7] not in {"0", "1"}:
        return None
    return origin, chat, fwd_msg, user, days, msg, parts[7] == "1"


def parse_deny(data: str) -> int | None:
    """`nPub {fwd_msg}` -> the id, or `None` for the bare `nPub`.

    v1: `if len(query_data.split()) < 2: return` (`:224-225`). The group prompt's
    ❌ carries no id and therefore evicts nothing; the approval chat's does.
    """
    parts = data.split()
    if len(parts) < 2 or parts[0] != _DENY_TOKEN:
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


def _is_submit(callback: CallbackQuery) -> bool:
    return parse_submit(callback.data or "") is not None


def _is_approve(callback: CallbackQuery) -> bool:
    return parse_approve(callback.data or "") is not None


def _is_deny(callback: CallbackQuery) -> bool:
    return (callback.data or "").split()[:1] == [_DENY_TOKEN]


# --------------------------------------------------------------------- /repost args


def parse_repost_days(args: str) -> int | None:
    """`schedule_autopost`'s day argument (`:298-306`).

    `None` means "not a valid number of days" — v1 tests `.isnumeric()` on the
    second word only, so `/repost 7 extra` is 7 days and `/repost -1` is
    rejected (`-` is not numeric). Absent argument -> the 9999 default, which
    the caller distinguishes by passing `""`.
    """
    parts = args.split()
    if not parts:
        return REPOST_UNLIMITED_DAYS
    if not parts[0].isnumeric():
        return None
    return int(parts[0])


def repost_schedule_time() -> tuple[int, int]:
    """`:310-311` — a daytime window, unlike the fan-out's all-day one."""
    return random.randint(10, 17), random.randint(0, 59)


# ------------------------------------------------------------------------ /divulgar


def _configured() -> bool:
    settings = get_settings()
    return bool(settings.postmail_chat_id and settings.approval_chat_id)


@router.message(CommandName("publish"), F.chat.type != ChatType.PRIVATE)
async def submit_post(message: Message, bot: Bot) -> None:
    """`/divulgar` `/publish` `/publicar`. v1: `ask_publisher_command` (`:57-75`).

    Open to everyone: v1 places no admin check and no feature-flag gate on this
    command, only on `/repost` below.
    """
    ctx = await context_for(bot, message)
    if not _configured():
        await message.reply(t(ctx, "publisher_unavailable"))
        return

    replied = message.reply_to_message
    if replied is None:
        await message.reply(t(ctx, "publish_need_reply"))
        return
    if replied.forward_from_chat is None or replied.forward_from_message_id is None:
        await message.reply(t(ctx, "publish_not_channel"))
        return
    if not replied.caption:
        await message.reply(t(ctx, "publish_needs_media"))
        return

    await _cache_post(replied)
    await _send_for_approval(
        bot,
        origin_chat_id=replied.forward_from_chat.id,
        requester_chat_id=message.chat.id,
        forward_from_message_id=replied.forward_from_message_id,
        requester_message_id=replied.message_id,
        requester_user_id=message.from_user.id if message.from_user else 0,
    )
    await message.reply(t(ctx, "publish_sent_for_approval"))


async def _cache_post(message: Message) -> None:
    """v1's `add_post_to_cache` (`:26-44`), against a real aiogram message."""
    resolved = publisher.resolve_pending_media(
        photo_file_id=message.photo[-1].file_id if message.photo else None,
        video_file_id=message.video.file_id if message.video else None,
        animation_file_id=message.animation.file_id if message.animation else None,
        document_file_id=message.document.file_id if message.document else None,
    )
    if resolved is None:
        log.warning("publisher.no_media_to_cache", message_id=message.message_id)
        return
    entity_urls = [e.url for e in (message.caption_entities or []) if e.url]
    await pending_posts.put(
        message.forward_from_message_id or message.message_id,
        publisher.pending_post_from(resolved, message.caption or "", entity_urls),
    )


async def _send_for_approval(
    bot: Bot,
    *,
    origin_chat_id: int,
    requester_chat_id: int,
    forward_from_message_id: int,
    requester_message_id: int,
    requester_user_id: int,
) -> None:
    """v1's `ask_approval` (`:77-92`): forward the requester's own message into
    the approval chat, then the five-button prompt."""
    approval_chat = get_settings().approval_chat_id
    with contextlib.suppress(Exception):
        # Best-effort, as v1's is: `forward_message` there swallows nothing but
        # is never checked either, and losing the preview must not lose the
        # approval prompt that follows it.
        await bot.forward_message(approval_chat, requester_chat_id, requester_message_id)
    await bot.send_message(
        approval_chat,
        APPROVE_PROMPT,
        reply_markup=_build_approval_keyboard(
            origin_chat_id=origin_chat_id,
            requester_chat_id=requester_chat_id,
            forward_from_message_id=forward_from_message_id,
            requester_user_id=requester_user_id,
            requester_message_id=requester_message_id,
        ),
    )


# ------------------------------------------------------------------------- callbacks


@router.callback_query(_is_submit)
async def on_submit_press(callback: CallbackQuery, bot: Bot) -> None:
    """The ✔️ on the group's own prompt. v1: `COOKIEBOT.py:370-371`.

    v1 deletes the message carrying the button before dispatching any of the
    three branches (`COOKIEBOT.py:367-369`), best-effort; same here.
    """
    parsed = parse_submit(callback.data or "")
    if parsed is None:  # pragma: no cover - the filter already matched
        await callback.answer()
        return
    origin_chat_id, requester_chat_id, forward_from_message_id, requester_message_id = parsed
    await _delete_prompt(callback)
    if not _configured():
        await callback.answer()
        return
    await _send_for_approval(
        bot,
        origin_chat_id=origin_chat_id,
        requester_chat_id=requester_chat_id,
        forward_from_message_id=forward_from_message_id,
        requester_message_id=requester_message_id,
        requester_user_id=callback.from_user.id,
    )
    await callback.answer()


@router.callback_query(_is_approve)
async def on_approve_press(callback: CallbackQuery) -> None:
    """The owner's approval. v1: `schedule_post` via `COOKIEBOT.py:372-373`.

    Everything v1 did inline here is now the `PUBLISHER_APPROVE` job: two
    translations, N exchange-rate calls, two media uploads and a row per
    consenting group (AGENTS.md §2.4).
    """
    parsed = parse_approve(callback.data or "")
    if parsed is None:  # pragma: no cover - the filter already matched
        await callback.answer()
        return
    if not _from_approval_chat(callback):
        # D-PF-2. Answered without a notice: telling a prober that the payload
        # was well-formed but the chat was wrong is more than they need.
        log.warning("publisher.approve_from_wrong_chat", data=callback.data)
        await callback.answer()
        return

    origin, requester_chat, fwd_msg, user, days, requester_msg, nsfw = parsed
    await _delete_prompt(callback)
    await enqueue(
        jobs.PUBLISHER_APPROVE,
        pending_key=str(fwd_msg),
        origin_chat_id=origin,
        requester_chat_id=requester_chat,
        requester_message_id=requester_msg,
        requester_user_id=user,
        days=days,
        has_nsfw=nsfw,
    )
    await callback.answer()


@router.callback_query(_is_deny)
async def on_deny_press(callback: CallbackQuery) -> None:
    """❌ on either prompt. v1: `deny_post` (`:223-228`).

    The group prompt's ❌ carries no id, so it evicts nothing — see
    `build_approval_request`. The approval chat's does, and only that one is
    authorised to, for the same reason the approve press is.
    """
    await _delete_prompt(callback)
    forward_from_message_id = parse_deny(callback.data or "")
    if forward_from_message_id is not None and _from_approval_chat(callback):
        await pending_posts.discard(forward_from_message_id)
    await callback.answer()


def _from_approval_chat(callback: CallbackQuery) -> bool:
    message = callback.message
    if message is None:
        return False
    return message.chat.id == get_settings().approval_chat_id


async def _delete_prompt(callback: CallbackQuery) -> None:
    """v1 `COOKIEBOT.py:367-369`: the button's message goes, whatever happens next."""
    message = callback.message
    if message is None or isinstance(message, InaccessibleMessage):
        return
    with contextlib.suppress(Exception):
        await message.delete()


# --------------------------------------------------------------------------- /repost


@router.message(CommandName("repost"), F.chat.type != ChatType.PRIVATE)
async def schedule_repost(message: Message, bot: Bot) -> None:
    """`/repost` `/repostar` `/reenviar`. v1: `schedule_autopost` (`:288-314`).

    Admin-gated, with v1's `ownerID` bypass (`:290`) — which `cancel_posts`
    deliberately does not have (`:318`). The asymmetry is v1's; both are ported
    as they are.
    """
    ctx = await context_for(bot, message)
    if not _is_repost_authorised(ctx, message):
        await message.reply(t(ctx, "not_group_admin"))
        return

    replied = message.reply_to_message
    if replied is None:
        await message.reply(t(ctx, "repost_need_reply"))
        return

    args = _command_args(message)
    days = parse_repost_days(args)
    if days is None:
        await message.reply(t(ctx, "repost_bad_days"))
        return

    hour, minute = repost_schedule_time()
    title = message.chat.title or ""
    await scheduled_posts.create(
        group_id=message.chat.id,
        origin_title=title,
        target_title=title,
        days_remaining=days,
        # v1 computes this inside `create_job` (`:96`): today at hour:minute,
        # plus a day. Duplicated rather than shared with the worker's copy —
        # the gateway must not import `cb_worker`.
        next_run_at=_next_run_at(hour, minute),
        source_chat_id=message.chat.id,
        source_message_id=replied.message_id,
        requester_chat_id=message.chat.id,
        requester_message_id=replied.message_id,
        requester_user_id=message.from_user.id if message.from_user else 0,
    )

    with contextlib.suppress(Exception):
        await message.react([ReactionTypeEmoji(emoji="👍")])
    if args.split():
        await message.reply(t(ctx, "repost_scheduled_days", days=days), parse_mode="HTML")
    else:
        await message.reply(t(ctx, "repost_scheduled_nolimit"), parse_mode="HTML")


def _is_repost_authorised(ctx: ChatContext, message: Message) -> bool:
    """v1 `:290`: an admin, the owner, or an anonymous sender.

    `ctx.is_admin` already covers v1's `'sender_chat' in msg` correctly — see
    `docs/contracts/admins.md` — so only the `ownerID` clause is extra.
    """
    if ctx.is_admin:
        return True
    owner_id = get_settings().owner_id
    return bool(owner_id and message.from_user is not None and message.from_user.id == owner_id)


def _command_args(message: Message) -> str:
    from cb_core.textmatch import parse_command

    parsed = parse_command(message.text or "")
    return parsed.args if parsed is not None else ""


def _next_run_at(hour: int, minute: int) -> datetime:
    """v1's `create_job` (`:96`): today at `hour:minute`, plus one day —
    unconditionally, so the first repost is always tomorrow even when the drawn
    time is still ahead today."""
    local = Instant.now().to_system_tz()
    return (
        local.replace(hour=hour, minute=minute, second=0, nanosecond=0).add(days=1).to_instant()
    ).to_stdlib()


# ----------------------------------------------------------------------- reply relay


def _is_post_reply(message: Message) -> bool:
    """v1 `COOKIEBOT.py:302`: a text reply to a bot message that has buttons."""
    replied = message.reply_to_message
    return (
        bool(message.text)
        and replied is not None
        and replied.from_user is not None
        and replied.from_user.is_bot
        and replied.reply_markup is not None
        and bool(replied.reply_markup.inline_keyboard)
    )


@relay_router.message(_is_post_reply, F.chat.type != ChatType.PRIVATE)
async def relay_reply_to_author(message: Message, bot: Bot) -> None:
    """`check_notify_post_reply` (`:359-369`) — carry the reply to the poster.

    v1 matches the *first* job whose name starts with the text of inline
    keyboard row 0 column 0, which `prepare_post` sets to the origin channel's
    title (`:185`). v2 stores that title in its own column, so the substring
    scan is an equality lookup. Every row v1 could match, this matches too.

    Raises `SkipHandler` on no match: a reply to any other bot message with
    buttons — a captcha, a `/config` menu — must carry on down the chain.
    """
    replied = message.reply_to_message
    if replied is None or replied.reply_markup is None:  # pragma: no cover - filter checked
        raise SkipHandler
    button_text = replied.reply_markup.inline_keyboard[0][0].text
    post = await scheduled_posts.find_by_origin_title(button_text)
    if post is None:
        raise SkipHandler

    ctx = await context_for(bot, message)
    author = message.from_user
    if author is not None and author.username:
        who = f"@{author.username}"
    elif author is not None:
        who = f"{author.first_name} {author.last_name or ''}".rstrip()
    else:
        who = ""
    body = f"{who} replied:\n'{message.text}'\n\nIn chat {message.chat.title or ''}"

    try:
        await bot.send_message(
            post.requester_chat_id, body, reply_to_message_id=post.requester_message_id
        )
    except Exception as exc:  # noqa: BLE001 - the poster's chat is the outside world
        # v1 has no handler here at all, so a deleted post or a chat the bot has
        # been removed from took the whole update down. Log and still confirm:
        # the group has no way to act on the difference.
        log.warning("publisher.relay_failed", error=str(exc))
    await message.reply(t(ctx, "notify_post_reply_sent"))


__all__ = [
    "APPROVAL_OPTIONS",
    "APPROVE_PROMPT",
    "REPOST_UNLIMITED_DAYS",
    "build_approval_request",
    "parse_approve",
    "parse_deny",
    "parse_repost_days",
    "parse_submit",
    "relay_router",
    "repost_schedule_time",
    "router",
]
