"""x_drawing_idea — `/ideiadesenho`, `/drawingidea`, `/ideadibujo`: a random
reference photo to draw from, captioned with the reference's id.

v1: `drawing_idea`,
`../COOKIEBOT-Telegram-Group-Bot/Bot/Miscellaneous.py:137-143`::

    def drawing_idea(cookiebot, msg, chat_id, language):
        send_chat_action(cookiebot, chat_id, 'upload_photo')
        idea_id = random.randint(0, len(bloblist_ideiadesenho)-1)
        blob = bloblist_ideiadesenho[idea_id]
        photo = blob.generate_signed_url(datetime.timedelta(minutes=15), method='GET')
        caption = i18n.get("drawing_idea", lang=language, idea_id=idea_id)
        send_photo(cookiebot, chat_id, photo, caption=caption, msg_to_reply=msg)

Dispatched `COOKIEBOT.py:248,253,256-257`, inside the `utilityfunctions`-gated
stretch — off answers `utility_off`, not silence. Full contract:
`docs/contracts/x_drawing_idea.md`.

## The id in the caption is a position, not an identity

`idea_id` is the **index** `random.randint` drew, printed straight into the
caption ("Reference ID 2814"). It is not stored anywhere and nothing looks one
up: there is no `/ideiadesenho 2814`, in v1 or here. Its only real use is a
person quoting the number when asking about a picture — which only works while
the pool's order is stable, and v1's was only as stable as a GCS listing.

This port keeps the same contract by taking the index into
`legacy_assets.entries_for("IdeiaDesenho")`, whose rows the catalog generator
sorts by `source_path` — the same lexicographic order `list_blobs` returns, so
the numbers a group has been quoting for years still land on the same images
as long as the export is complete. Nothing here *guarantees* that beyond the
sort: a re-export that dropped an object would shift every id after it, in
exactly the way deleting a blob shifted v1's own.

3,435 references, 789 MB, the largest of the six exported prefixes.
"""

from __future__ import annotations

import random
from posixpath import splitext
from typing import cast

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.types import BufferedInputFile, Message

from cb_core import legacy_assets, storage
from cb_core.logging import get_logger
from cb_gateway.context import context_for, deny_if_disabled, t
from cb_gateway.filters import CommandName

log = get_logger("cb.gateway.drawing_idea")

router = Router(name="drawing_idea")

#: `Miscellaneous.py:16` — the v1 bucket prefix, and the `legacy_assets`
#: catalog key it was exported under.
_LEGACY_PREFIX = "IdeiaDesenho"

_rng = random.Random()


def pick_reference(
    entries: tuple[legacy_assets.LegacyAsset, ...], rng: random.Random | None = None
) -> tuple[int, legacy_assets.LegacyAsset] | None:
    """`(idea_id, entry)` — v1's `random.randint(0, len(pool)-1)` and the row
    it indexes (`:139-140`), or `None` for an empty pool.

    Returning the index alongside the row is the whole point: the caption
    prints it (module docstring), so the draw cannot be delegated to
    `legacy_assets.choose`, which returns a row and forgets where it came
    from. v1's own `randint(0, -1)` on an empty pool raised `ValueError`;
    `None` here is `fun_death`'s D-DE-3 decision applied to the same shape.
    """
    if not entries:
        return None
    picker = rng or _rng
    index = picker.randint(0, len(entries) - 1)
    return index, entries[index]


@router.message(F.chat.type != ChatType.PRIVATE, CommandName("drawingidea"))
async def drawing_idea(message: Message) -> None:
    """`/ideiadesenho`, `/drawingidea`, `/ideadibujo` — all three map to the
    `drawingidea` canonical name in `cb_core/textmatch.py:COMMAND_ALIASES`."""
    bot = cast(Bot, message.bot)
    ctx = await context_for(bot, message)
    if await deny_if_disabled(message, ctx, "utility"):
        return

    # v1 signals "uploading a photo" before it has picked anything (`:138`).
    await bot.send_chat_action(message.chat.id, "upload_photo")

    picked = pick_reference(legacy_assets.entries_for(_LEGACY_PREFIX))
    if picked is None:
        log.warning("drawing_idea.pool_empty", prefix=_LEGACY_PREFIX)
        return
    idea_id, entry = picked

    data = await storage.store().get(entry.storage_key)
    _, extension = splitext(entry.source_path)
    photo = BufferedInputFile(data, filename=f"drawingidea{extension}")
    await message.reply_photo(photo, caption=t(ctx, "drawing_idea", idea_id=idea_id))


__all__ = ["drawing_idea", "pick_reference", "router"]
