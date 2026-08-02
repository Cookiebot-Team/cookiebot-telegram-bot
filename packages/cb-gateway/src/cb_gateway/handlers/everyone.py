"""util_everyone — `/everyone`, bare `@everyone`, and QA's `/ping everyone` spelling.

v1: `everyone`, `../COOKIEBOT-Telegram-Group-Bot/Bot/UserRegisters.py:97-146`,
dispatched `COOKIEBOT.py:272-273` (`msg['text'].startswith(("/everyone",
"@everyone"))`, reached from the group-message branch and, unlike its
neighbours, never gated on `utilityfunctions`)::

    def everyone(cookiebot, msg, chat_id, listaadmins, language, is_alternate_bot=0):
        send_chat_action(cookiebot, chat_id, 'typing')
        if len(listaadmins) > 0 and 'from' in msg and str(msg['from']['username']) \
                not in listaadmins and 'sender_chat' not in msg:
            text = i18n.get("everyone_no", lang=language)
            send_message(cookiebot, chat_id, text, msg)
            return
        members = get_members_chat(cookiebot, chat_id)
        usernames_list = [member['user'] for member in members if 'user' in member]
        usernames_list.extend(admin for admin in listaadmins if admin not in usernames_list)
        if len(usernames_list) < 2:
            text = i18n.get("everyone_len", lang=language)
            send_message(cookiebot, chat_id, text, msg)
            return
        react_to_message(msg, '🫡', is_alternate_bot=is_alternate_bot)
        result = [f"Number of known users: {min(len(usernames_list), cookiebot.getChatMembersCount(chat_id))}\n"]
        for username in usernames_list:
            try:
                if len(result[top_message_index]) + len(username) + 2 > 4096:
                    result.append("")
                    top_message_index += 1
            except TypeError:
                pass
            result[top_message_index] += f"@{username} "
        for resulting_message in result:
            send_message(cookiebot, chat_id, resulting_message, msg_to_reply=msg, parse_mode='HTML')
        # ... then the per-member DM loop: cb_worker/jobs/everyone.py, not here.

Contract: `docs/contracts/util_everyone.md`. QA:
`../Cookiebot-QA/features/util_everyone.feature` -> `qa/features/util_everyone.feature`.

## Admin gate — a deliberate divergence from v1 (D-EV-2 / D-EV-3)

v1's gate only rejects when *all four* hold: the admin list is non-empty, the
message carries `from.username`, that username is not in the admin list, and
there is no `sender_chat`. Two consequences follow, and `/migrate-feature`
Phase 2's rule is that a silent-failure bug gets fixed, not preserved as a
quirk:

* **D-EV-2** — a failed `getChatAdministrators` leaves `listaadmins` empty,
  which skips the `len(listaadmins) > 0` check entirely and turns `/everyone`
  into a free-for-all.
* **D-EV-3** — a caller with **no** `username` always passes, regardless of
  admin status, because the gate's own condition requires `from.username` to
  exist before it can even compare it.

v2 fails **closed** instead: `ctx.is_admin` (`cb_gateway/context.py` ->
`cb_core/admins.py:resolve_actor`) is already `False` both for "confirmed
non-admin" and for "no admin status could be established" — an anonymous
`sender_chat` sender is the one case that is trusted unconditionally, matching
v1's `'sender_chat' not in msg` bypass (`cb_core/admins.py`'s module
docstring). One `if not ctx.is_admin` covers both v1 gaps at once; there is no
"admin list empty" or "no username" special case left to reproduce.

## Chunking — D-EV-4 (preserved) and D-EV-6 (dropped)

`ping_chunks` is v1's manual chunker (`UserRegisters.py:112-120`) as a pure,
Telegram-free function. The `Number of known users: {known}` header is
hardcoded English, never localised, and appears once on the first chunk only
(D-EV-4, `:112`) — deliberately preserved, since localising it would change
what every existing group sees. The boundary check reproduces v1's exact
arithmetic, off-by-two included: `len(current) + len(username) + 2 > 4096`,
not the "obviously correct" `len(current) + len(f"@{{username}} ")`. v1's
`try/except TypeError: pass` around that check is not reproduced (D-EV-6): a
`str`'s `len()` never raises `TypeError`, so the guard caught nothing.

## What is not here

D-EV-5 (every 10th DM forwarding the group message to the bot owner) is
dropped outright — undisclosed exfiltration of group content, no
configuration, no v2 equivalent (see `cb_worker/jobs/everyone.py`). The DM
loop itself is not on the reply path at all: `roster` fixes v1's N+1 backend
call (`:129`, D-EV-1) by reading `group_members` once, but the fan-out is
still per-member Telegram I/O, and AGENTS.md §2.4 puts multi-chat fan-out in
`cb-worker`, never the gateway. This handler enqueues
`jobs.EVERYONE_FANOUT` (scalars only — R4.7) and returns; no DM, no
`getChatMember` call happens here.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Sequence
from typing import cast

from aiogram import Bot, F, Router
from aiogram.types import Message, ReactionTypeEmoji

from cb_core import jobs, members
from cb_gateway.context import context_for, t
from cb_gateway.filters import CommandName
from cb_gateway.queue import enqueue
from cb_gateway.telemetry import mark_outcome

router = Router(name="everyone")

# Telegram's hard cap on one `sendMessage` call's `text` field.
_CHUNK_LIMIT = 4096

# v1's bare `startswith(("/everyone", "@everyone"))` (`COOKIEBOT.py:272`) folds
# the mention form into the same prefix check as the slash form; `parse_command`
# only ever inspects `/`-prefixed text (see `calladms.py:145-152`'s identical
# note for `@admin`/`@adm`), so the bare-word form needs its own matcher here.
_MENTION_TRIGGER = re.compile(r"^@everyone\b", re.IGNORECASE)


def _is_mention_trigger(message: Message) -> bool:
    return bool(message.text and _MENTION_TRIGGER.match(message.text))


def ping_chunks(usernames: Sequence[str], known: int) -> list[str]:
    """v1's manual chunker, minus its dead defensive code (`UserRegisters.py:112-120`).

    One `f"Number of known users: {known}\\n"` header on the first chunk only
    (D-EV-4), then `f"@{username} "` appended per member. A new chunk starts
    when the append *would* push the current chunk over `_CHUNK_LIMIT` —
    `len(current) + len(username) + 2 > _CHUNK_LIMIT`, byte-identical to v1's
    condition, off-by-two included, so the chunk boundary lands on the same
    username v1's would.
    """
    chunks = [f"Number of known users: {known}\n"]
    top = 0
    for username in usernames:
        if len(chunks[top]) + len(username) + 2 > _CHUNK_LIMIT:
            chunks.append("")
            top += 1
        chunks[top] += f"@{username} "
    return chunks


@router.message(_is_mention_trigger, F.chat.type.in_({"group", "supergroup"}))
@router.message(CommandName("everyone"), F.chat.type.in_({"group", "supergroup"}))
async def everyone(message: Message, bot: Bot | None = None) -> None:
    """`/everyone` and bare `@everyone` (v1, both above); the QA spelling
    `/ping everyone` does not reach this handler — see the module the tests
    document that gap against, `docs/contracts/util_everyone.md`.
    """
    active_bot = cast(Bot, bot or message.bot)
    ctx = await context_for(active_bot, message)

    # D-EV-2 / D-EV-3 (see module docstring): fail closed rather than v1's
    # fail-open gate. `ctx.is_admin` already reads False for an unresolvable
    # actor, so this one check covers both v1 gaps.
    if not ctx.is_admin:
        mark_outcome("refused")
        await message.reply(t(ctx, "everyone_no"))
        return

    roster = await members.roster(ctx.group_id)
    usernames = [member.username for member in roster if member.username is not None]
    if len(usernames) < 2:
        # v1's `len(usernames_list) < 2` (`UserRegisters.py:107`).
        mark_outcome("refused")
        await message.reply(t(ctx, "everyone_len"))
        return

    await active_bot.send_chat_action(ctx.group_id, "typing")
    with contextlib.suppress(Exception):
        # Best-effort like every other reaction in this codebase (`ship.py`,
        # `firecracker.py`) — v1's own `react_to_message` has no error handling
        # either, and its caller swallows everything (`COOKIEBOT.py:329`).
        await message.react(reaction=[ReactionTypeEmoji(emoji="🫡")])

    known = min(len(usernames), await active_bot.get_chat_member_count(ctx.group_id))
    for chunk in ping_chunks(usernames, known):
        await message.reply(chunk, parse_mode="HTML")

    # The DM fan-out is cb-worker's job, not the reply path's (AGENTS.md §2.4,
    # module docstring). Scalars only (R4.7) — the worker re-reads the roster
    # rather than trusting a member list shipped through the broker.
    await enqueue(
        jobs.EVERYONE_FANOUT,
        group_id=ctx.group_id,
        chat_id=message.chat.id,
        message_id=message.message_id,
        chat_title=message.chat.title or "",
        lang=ctx.lang,
    )


__all__ = ["everyone", "ping_chunks", "router"]
