"""x_custom_commands — `/<name>` for any `name` that has a picture pool, v1's
`Custom/` bucket prefix.

v1: `custom_command`,
`../COOKIEBOT-Telegram-Group-Bot/Bot/Miscellaneous.py:145-158`, dispatched
`COOKIEBOT.py:281-282`::

    def custom_command(cookiebot, msg, chat_id, language):
        send_chat_action(cookiebot, chat_id, 'upload_photo')
        bloblist = list(storage_bucket.list_blobs(prefix="Custom/"+msg['text']...split()[0]))
        if len(msg['text'].split()) > 1 and msg['text'].split()[1].isdigit():
            image_id = int(msg['text'].split()[1])
        else:
            image_id = random.randint(0, len(bloblist)-1)
        ...
        caption = i18n.get("custom_photo", lang=language, **ctx)
        send_photo(cookiebot, chat_id, photo, msg_to_reply=msg, caption=caption)

Full contract: `docs/contracts/x_custom_commands.md`. Spec/design:
`.specs/features/x_custom_commands/`.

## The command names are data

`custom_commands` in v1 is not a list in the source — it is
`[folder.name.split('/')[1] for folder in list_blobs(prefix="Custom/")]`
evaluated once at import (`Miscellaneous.py:23`). 53 folders came out of the
export; they are `cb_core.legacy_assets.custom_command_names()` now, and
`CustomCommandName` matches against them rather than against
`COMMAND_ALIASES`, which cannot hold a name that is data. That is also why
this router is registered *last* among the command routers: it must never
shadow a real command whose name happens to collide with a folder.

## Selection, and the index v1 did not bound

`/<name> <n>` sends image `n`; anything else draws at random. Both v1's, and
`n` is an index into the same alphabetically-sorted pool `x_drawing_idea`'s
caption id indexes, for the same reason: it is what a person quotes back.

v1 never bounds-checked it (`bloblist[image_id]` on a user-supplied integer,
D-CC-1), so `/<name> 999` raised `IndexError` into the dispatcher's bare
`except` and the group saw nothing at all. This port answers an out-of-range
index with the same *observable* outcome — nothing sent — and a
`custom_command.index_out_of_range` log instead of a traceback. Clamping was
the other candidate and is worse: it would send a picture while the caption
claimed a different id.

## Per-tenant

This is the first family that `cb_gateway.packs` gates on `tenant.handler_pack`
— see that module for why one dispatcher plus a filter, rather than the
per-pack dispatchers `multi-tenant.mdx` originally sketched. A brand that
wants none of these 53 commands sets `handler_pack = 'minimal'`; a brand that
wants most of them uses `disabled_commands` as usual.
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
from cb_gateway.filters import CustomCommandName
from cb_gateway.packs import LEGACY_CUSTOM, PackProvides

log = get_logger("cb.gateway.custom_command")

router = Router(name="custom_command")

_rng = random.Random()


def parse_index(args: str) -> int | None:
    """v1: `if len(msg['text'].split()) > 1 and msg['text'].split()[1].isdigit()`
    (`Miscellaneous.py:148-149`) — the *second whitespace token only*, and
    `str.isdigit()`, which is False for `"-1"` and `"1.5"` and True for
    non-ASCII decimal digits (Arabic-Indic, Devanagari, ...). `int()` accepts
    those too, so v1 indexed with them and so does this; the alternative is a
    stricter parse than v1's, which is a behaviour change for the sake of
    tidiness.
    """
    tokens = args.split()
    if not tokens or not tokens[0].isdigit():
        return None
    return int(tokens[0])


def display_name(name: str) -> str:
    """v1: `msg['text'].replace('/', '').replace('@CookieMWbot', '').split()[0]
    .capitalize()` (`:153`) — the caption's `%(name)s`.

    `.capitalize()`, not `.title()`: `"tailslunar"` reads back as
    `"Tailslunar"`, and a folder named `"MrNatMax"` would read `"Mrnatmax"`.
    Preserved — it is what a caption says today.
    """
    return name.capitalize()


@router.message(
    F.chat.type != ChatType.PRIVATE,
    PackProvides(LEGACY_CUSTOM),
    CustomCommandName(),
)
async def custom_command(message: Message, custom: tuple[str, str] | None = None) -> None:
    """`/<name>` where `name` is one of the exported `Custom/` folders."""
    if custom is None:  # pragma: no cover - the filter always injects it
        return
    name, args = custom

    bot = cast(Bot, message.bot)
    ctx = await context_for(bot, message)
    # v1 gates this on `funfunctions` (`COOKIEBOT.py:281`). Note what happens
    # in v1 when fun is off: the `elif` chain falls through to the image-search
    # catch-all two lines down, so `/tailslunar` becomes a Google image search
    # for "tailslunar" rather than answering `fun_off`. x_image_search's own
    # port is where that fall-through is reproduced; here the gate replies, the
    # way every other fun command does.
    if await deny_if_disabled(message, ctx, "fun"):
        return

    await bot.send_chat_action(message.chat.id, "upload_photo")

    pool = legacy_assets.entries_for_custom(name)
    if not pool:  # pragma: no cover - the filter only matches non-empty pools
        return

    index = parse_index(args)
    if index is None:
        index = _rng.randint(0, len(pool) - 1)
    elif index >= len(pool):
        # D-CC-1: v1 raised IndexError here and the group saw nothing. Same
        # outcome, no traceback (module docstring).
        log.info("custom_command.index_out_of_range", command=name, index=index, pool=len(pool))
        return

    entry = pool[index]
    data = await storage.store().get(entry.storage_key)
    _, extension = splitext(entry.source_path)
    photo = BufferedInputFile(data, filename=f"{name}{extension}")
    caption = t(ctx, "custom_photo", name=display_name(name), image_id=index)
    await message.reply_photo(photo, caption=caption)


__all__ = ["custom_command", "display_name", "parse_index", "router"]
