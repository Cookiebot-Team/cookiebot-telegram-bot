"""A context for private chats — deliberately not `ChatContext` with an
optional `group_id`.

v1's only private-chat handling lives in one `if chat_type == 'private':`
block (`../COOKIEBOT-Telegram-Group-Bot/Bot/COOKIEBOT.py:73-110`) that returns
unconditionally at the end, so nothing group-shaped ever runs for a DM in v1
either. v2 had no equivalent at all — `HANDOFF.md` §1 gap 2 — and the gap was
not hypothetical: `cb_gateway/handlers/privacy.py` answered a DM `/privacy` by
calling `cb_gateway.context.context_for`, which reads `group_id =
message.chat.id` and queries `group_configs` (distributed on `group_id`) with
a private chat's own id — a "group" that never existed and never will. See
`.specs/features/private_dispatch/spec.md`'s "The live bug" for the full story.

`PrivateContext` carries `user_id` and nothing else — no `group_id`, no
`GroupConfig`, no `ActorCheck`. That absence is the point: a type that cannot
hold a `group_id` cannot be passed to `cb_core.group_config.get_config`,
`cb_core.admins.*` or `cb_core.members.*` and get a plausible-looking wrong
answer back, which is exactly what happened above. `lang` is deliberately not
here either — v1 uses two different DM language conventions (`pv_default_message`
derives one per sender, `/privacy`/`/commands` hardcode English), and a single
field would have to guess which a caller wants. Handlers that need v1's
hardcoded-English behaviour say `"en"` at the call site (`privacy.py`,
`listcommand.py`); a real second convention gets a real second field when it
has a real consumer (`/start`, not built yet — `spec.md`'s named follow-up).

`private_context_for` is synchronous on purpose: everything it reads is
already on the `Message` object aiogram handed the handler. There is nothing
to `await` because there is nothing to query — the concrete, type-level proof
that this module cannot accidentally issue a distributed-table read.
"""

from __future__ import annotations

from dataclasses import dataclass

from aiogram.types import Message


@dataclass(frozen=True, slots=True)
class PrivateContext:
    user_id: int


def private_context_for(message: Message) -> PrivateContext:
    """`user_id` for the private chat `message` arrived in.

    Telegram always includes `from` on a message sent by a real user in a
    private chat (the only kind of update this is ever called with — every
    call site is behind a `F.chat.type == ChatType.PRIVATE` filter); the
    fallback to `message.chat.id` exists only because a DM's chat id and the
    sender's user id are the same number by Telegram's own convention, so it
    is never wrong, just belt-and-braces for the type checker.
    """
    from_user = message.from_user
    return PrivateContext(user_id=from_user.id if from_user is not None else message.chat.id)


__all__ = ["PrivateContext", "private_context_for"]
