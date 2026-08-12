"""x_analysis — `/analise`, `/analisis`, `/analysis`: dump a replied-to message's
raw Telegram payload back into the chat.

v1: `analyze`, `../COOKIEBOT-Telegram-Group-Bot/Bot/Miscellaneous.py:71-81`::

    def analyze(cookiebot, msg, chat_id, language, is_alternate_bot=0):
        send_chat_action(cookiebot, chat_id, 'typing')
        if not 'reply_to_message' in msg:
            text = i18n.get("analyze", lang=language)
            send_message(cookiebot, chat_id, text, msg)
            return
        react_to_message(msg, '🤔', is_alternate_bot=is_alternate_bot)
        result = ''
        for item in msg['reply_to_message']:
            result += str(item) + ': ' + str(msg['reply_to_message'][item]) + '\\n'
        send_message(cookiebot, chat_id, result, msg_to_reply=msg)

Dispatched at `COOKIEBOT.py:202-203`, in the ungated stretch of the chain that
also holds `/privacy` and `/reload` — no `functionsFun`, no `functionsUtility`,
no admin check. It answers for anyone, in any group, which is why this handler
has no gate either.

It is a debugging tool that shipped: what a member actually gets is the
file_id, the message_id and the sender fields of whatever they replied to,
which is exactly what someone reporting "the bot won't take my sticker" needs
to paste. `config_menu.py:188` already tells users to find a value "with
/analysis command" — that instruction was pointing at a command v2 did not
have.

## Two places this deviates, and why

1. **The payload is the model's, not a captured JSON dict.** v1 iterated the
   raw dict it got from `getUpdates`; aiogram hands handlers a parsed
   `Message`, so this dumps `model_dump(exclude_none=True)` — the same fields
   Telegram sent, since aiogram's models mirror the Bot API, with absent ones
   omitted the way they were absent from v1's dict too. Key *order* follows
   aiogram's field declaration rather than Telegram's JSON, which nothing can
   depend on: v1's own order was whatever the JSON parser produced.

2. **It truncates.** Telegram caps a message at 4096 characters and v1 did not
   check: a reply to a message with a large `entities` array or a long
   forwarded chain made `sendMessage` fail with a 400, so the command silently
   did nothing on exactly the messages worth analysing. Truncated at 4000 plus
   a marker, the same limit and reasoning `transcribe.py` already uses.
"""

from __future__ import annotations

from typing import Any, cast

from aiogram import Bot, Router
from aiogram.types import Message, ReactionTypeEmoji

from cb_core.logging import get_logger
from cb_gateway.context import context_for, t
from cb_gateway.filters import CommandName

log = get_logger("cb.gateway.analysis")

router = Router(name="analysis")

#: v1 sends the whole dump in one message; Telegram's own limit is 4096. Same
#: budget `transcribe.py` picked, leaving room for the marker below.
_MAX_REPLY_CHARS = 4000

_TRUNCATED = "\n… (truncated)"


def render_payload(payload: dict[str, Any], *, limit: int = _MAX_REPLY_CHARS) -> str:
    """`key: value` per line, v1's exact shape (`str(item) + ': ' + str(value)`).

    Rendering is a pure function of the payload so the one thing worth
    asserting — that a payload too large for Telegram comes back short enough
    to send, with the marker — is testable without a Bot, a chat or a network.
    """
    body = "".join(f"{key}: {value}\n" for key, value in payload.items())
    if len(body) <= limit:
        return body
    return body[: limit - len(_TRUNCATED)] + _TRUNCATED


@router.message(CommandName("analysis"))
async def analysis(message: Message) -> None:
    target = message.reply_to_message
    if target is None:
        ctx = await context_for(cast(Bot, message.bot), message)
        await message.reply(t(ctx, "analyze"))
        return

    try:
        # v1 reacts to the *command* message, not to the one being analysed
        # (`react_to_message(msg, ...)`, where `msg` is the command). Kept, and
        # kept non-fatal: reactions fail for a bot without the permission, and
        # v1's own helper swallowed that too — losing the dump because the 🤔
        # did not land would be a worse trade than losing the 🤔.
        await message.react([ReactionTypeEmoji(emoji="🤔")])
    except Exception as exc:  # noqa: BLE001 - a missing reaction must not cost the answer
        log.info("analysis.react_failed", error=str(exc))

    await message.reply(render_payload(target.model_dump(exclude_none=True)))


__all__ = ["analysis", "render_payload", "router"]
