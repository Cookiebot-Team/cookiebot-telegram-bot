"""util_postgetter — offer to share a channel post Telegram just auto-forwarded.

v1: `ask_publisher` (`../COOKIEBOT-Telegram-Group-Bot/Bot/Publisher.py:46-55`),
dispatched from the media branch of the content-type chain
(`COOKIEBOT.py:165-166`) under a six-way conjunction reproduced by the filter
below.

Spec: `.specs/features/util_postgetter/`. Contract:
`docs/contracts/util_postgetter.md`.

**Registration order is behaviour.** v1's branch is an `elif` that sits *ahead*
of the `photo`/`video` branches which pool media into the group's random library
(`COOKIEBOT.py:167-172`), so an auto-forwarded ad is never also collected by
`fun_random`. aiogram reproduces that only if this router is registered before
`fun_random.router` and this handler *replies* — completing without
`SkipHandler` is what stops propagation. Register it after `fun_random` and
every ad silently joins the random pool.

The other half of this feature — whether scheduled posts are delivered into this
group at all, into which forum topic, and how many campaigns may target it —
lives where it can be enforced: `cb_worker/jobs/publisher.py`'s delivery sweep
and `util_postforwarder`'s fan-out. See this feature's contract for the table.
"""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.enums import ChatType
from aiogram.types import Message
from prometheus_client import Counter

from cb_core import pending_posts, publisher
from cb_core.logging import get_logger
from cb_gateway.context import context_for, t
from cb_gateway.handlers.publisher import build_approval_request

log = get_logger("cb.postgetter")

router = Router(name="postgetter")

# outcome in prompted|disabled. No group label (AGENTS.md §7).
publisher_ask_total = Counter(
    "cb_gateway_publisher_ask_total",
    "Auto-forwarded channel posts the bot offered to share",
    ["outcome"],
)

#: v1's discriminator for "Telegram auto-forwarded this from the linked channel"
#: (`COOKIEBOT.py:165`). It is a literal comparison against the sender's first
#: name, not a check on the user id `777000` — that would be a different
#: predicate, and an untested one. Ported as the comparison v1 evaluates.
TELEGRAM_SENDER_FIRST_NAME = "Telegram"


def _is_auto_forwarded_ad(message: Message) -> bool:
    return (
        message.from_user is not None
        and message.from_user.first_name == TELEGRAM_SENDER_FIRST_NAME
        and message.forward_from_message_id is not None
    )


@router.message(
    _is_auto_forwarded_ad,
    F.chat.type != ChatType.PRIVATE,
    F.photo | F.video | F.animation | F.document,
    F.sender_chat,
    F.forward_from_chat,
    F.caption,
)
async def offer_to_share(message: Message, bot: Bot) -> None:
    """v1's `ask_publisher` (`:46-55`).

    `publisher_ask` defaults to on (`Configurations.py:111`, and
    `group_config.py:64`). Off means this handler was never reached in v1's
    `elif` chain either, so the message must continue down the chain here —
    `SkipHandler`, not a quiet return.
    """
    ctx = await context_for(bot, message)
    if not ctx.config.publisher_ask:
        publisher_ask_total.labels(outcome="disabled").inc()
        raise SkipHandler

    await _cache(message)
    # `t()` resolves `publisher_ask_prompt` to English for an `es` group, because
    # the key is deliberately absent from the Spanish catalog: v1's ternary is
    # `"Divulgar postagem?" if pt else "Share post?"` with no Spanish arm at all
    # (D-PG-3). The omission is the port; copying the English string into the
    # `es` file would hide it from anyone diffing the catalogs.
    await message.reply(
        t(ctx, "publisher_ask_prompt"),
        reply_markup=build_approval_request(
            origin_chat_id=message.forward_from_chat.id if message.forward_from_chat else 0,
            chat_id=message.chat.id,
            forward_from_message_id=message.forward_from_message_id or 0,
            message_id=message.message_id,
        ),
    )
    publisher_ask_total.labels(outcome="prompted").inc()


async def _cache(message: Message) -> None:
    """v1's `add_post_to_cache` (`:26-44`), keyed on `forward_from_message_id`."""
    resolved = publisher.resolve_pending_media(
        photo_file_id=message.photo[-1].file_id if message.photo else None,
        video_file_id=message.video.file_id if message.video else None,
        animation_file_id=message.animation.file_id if message.animation else None,
        document_file_id=message.document.file_id if message.document else None,
    )
    if resolved is None:  # pragma: no cover - the filter guarantees one of the four
        log.warning("postgetter.no_media", message_id=message.message_id)
        return
    entity_urls = [e.url for e in (message.caption_entities or []) if e.url]
    await pending_posts.put(
        message.forward_from_message_id or message.message_id,
        publisher.pending_post_from(resolved, message.caption or "", entity_urls),
    )


__all__ = ["publisher_ask_total", "router"]
