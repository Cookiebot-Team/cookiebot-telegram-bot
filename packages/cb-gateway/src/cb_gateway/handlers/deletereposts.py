"""util_deletereposts — `/deleteposts` cancels the posts this group asked for.

v1: `cancel_posts` (`../COOKIEBOT-Telegram-Group-Bot/Bot/Publisher.py:316-327`),
dispatched at `COOKIEBOT.py:209-211`. Triggers `/deleteposts` and
`/apagarposts` (the chain lists the latter twice and never ships QA's
`/deletereposts`); all three resolve through `COMMAND_ALIASES`.

Spec: `.specs/features/util_deletereposts/`. Contract:
`docs/contracts/util_deletereposts.md`.

Two things this command is *not*:

* It does not delete messages. v1 removes scheduled, not-yet-sent rows and
  touches nothing already delivered — QA's scenario reads the other way, and v1
  wins for observable behaviour (AGENTS.md §1).
* It has no `owner_id` bypass. `/repost` has one (`:290`) and this does not
  (`:318`). The asymmetry is v1's; both are ported as they are.
"""

from __future__ import annotations

import contextlib

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.types import Message, ReactionTypeEmoji
from prometheus_client import Counter

from cb_core import scheduled_posts
from cb_core.logging import get_logger
from cb_gateway.context import context_for, t
from cb_gateway.filters import CommandName

log = get_logger("cb.deletereposts")

router = Router(name="deletereposts")

# outcome in cancelled|denied. Row counts go to the log, never a label
# (AGENTS.md §7).
deletereposts_total = Counter(
    "cb_gateway_deletereposts_total", "/deleteposts invocations", ["outcome"]
)


@router.message(CommandName("deletereposts"), F.chat.type != ChatType.PRIVATE)
async def cancel_posts(message: Message, bot: Bot) -> None:
    """v1's `cancel_posts` (`:316-327`).

    `ctx.is_admin` is narrower than v1's `'sender_chat' in msg`, which accepted
    *any* anonymous sender rather than only an anonymous admin (D-DR-2).
    `cb_core.admins.resolve_actor` resolves that correctly and is what every
    other ported admin command already uses — see `docs/contracts/admins.md`.
    """
    ctx = await context_for(bot, message)
    if not ctx.is_admin:
        deletereposts_total.labels(outcome="denied").inc()
        await message.reply(t(ctx, "not_group_admin"))
        return

    # Filtered on `requester_chat_id`, which is deliberately not the
    # distribution column, so Citus fans this out to every shard. There is no
    # correct `group_id` predicate: the rows this cancels are spread across
    # every group the campaign targeted. Index-backed single-table DML, not a
    # repartition join, and at most one per admin invocation — AGENTS.md §4's
    # rule for this case is "say so", which is what this comment is.
    removed = await scheduled_posts.delete_by_requester(message.chat.id)
    log.info("publisher.reposts_cancelled", group_id=message.chat.id, count=removed)

    with contextlib.suppress(Exception):
        # v1 reacts before it replies (`:325-327`), and `react_to_message`
        # defaults to `is_big=True` (`universal_funcs.py:300`).
        await message.react([ReactionTypeEmoji(emoji="👍")])
    await message.reply(t(ctx, "deletereposts_done"))
    deletereposts_total.labels(outcome="cancelled").inc()


__all__ = ["deletereposts_total", "router"]
