"""Member registry bookkeeping — the write side of `cb_core.members`.

v1 ran this before dispatch, for every message: `COOKIEBOT.py:118` calls
`check_new_name(cookiebot, msg, chat_id, chat_type)`
(`../COOKIEBOT-Telegram-Group-Bot/Bot/UserRegisters.py:64-88`) on the way in, so
by the time any command handler asked "who is in this group?" the sender was
already registered. The register is what `/shippar`, `/everyone` and the birthday
commands all read.

Two updates matter:

- **any message from a user** -> upsert the user and their membership
- **`left_chat_member`** -> stamp `left_at` (v1 deleted the register entry,
  `UserRegisters.py:92-96`)

Both are bookkeeping, never a reply, so this always raises `SkipHandler` and is
registered first — the same contract `mediarestrict.record_join` and
`fun_random.pool_media` already follow, and for the same reason: a handler that
consumed the update here would silently swallow every command in the bot.

Private chats are skipped. v1's `check_new_name` does run in a DM, but only its
`users` half — the register half is explicitly `if chat_type in ['group',
'supergroup']` (`:85`) — and v2 has no private-chat dispatch at all yet
(`HANDOFF.md` §1, "known gaps"), so there is no DM update reaching this router
to record.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.enums import ChatType
from aiogram.types import Message, User

from cb_core import members
from cb_core.logging import get_logger

log = get_logger("cb.gateway.members")

router = Router(name="members")


def identity_of(user: User) -> members.MemberIdentity:
    """The five fields v1 read off `msg['from']` (`UserRegisters.py:67-71`),
    plus `is_bot` — recorded but not filtered on, see `cb_core.members`."""
    return members.MemberIdentity(
        user_id=user.id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
        language_code=user.language_code,
        is_bot=user.is_bot,
    )


@router.message(F.chat.type != ChatType.PRIVATE)
async def register_sender(message: Message) -> None:
    """Records the sender, then yields. Never replies, never consumes.

    `cb_core.members.record` swallows its own database errors (a registry write
    must not cost a user their command), so the only thing left to guard here is
    a genuinely unexpected failure — an aiogram payload shape this does not
    expect — which must still not take the update down.
    """
    try:
        if message.left_chat_member is not None:
            await members.mark_left(message.chat.id, message.left_chat_member.id)
        if message.from_user is not None:
            await members.record(message.chat.id, identity_of(message.from_user))
    except Exception as exc:  # noqa: BLE001 - bookkeeping never breaks the reply path
        log.warning("members.record_failed", error=str(exc))
    raise SkipHandler


__all__ = ["identity_of", "register_sender", "router"]
