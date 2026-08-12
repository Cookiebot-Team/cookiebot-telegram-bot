"""fun_death — `/death`, `/morte`, `/muerte`: a random "cause of death" for the
caller, a tagged name, or whoever they replied to.

v1: `death`, `../COOKIEBOT-Telegram-Group-Bot/Bot/Miscellaneous.py:335-357`::

    def death(cookiebot, msg, chat_id, language):
        react_to_message(msg, '👻')
        send_chat_action(cookiebot, chat_id, 'upload_photo')
        fileblob = bloblist_death[random.randint(0, len(bloblist_death)-1)]
        filename = fileblob.name
        fileurl = fileblob.generate_signed_url(datetime.timedelta(minutes=15), method='GET')
        if len(msg['text'].split()) > 1:
            caption = '💀💀💀 ' + msg['text'].split()[1]
        elif 'reply_to_message' in msg:
            caption = '💀💀💀 ' + msg['reply_to_message']['from']['first_name']
        else:
            caption = '💀💀💀 ' + '@'+msg['from']['username'] if 'username' in msg['from'] else msg['from']['first_name']
        variants = i18n.get("death.variants", lang=language)
        template = i18n.get("death.template", lang=language, variant=random.choice(variants))
        caption += template
        line = i18n.get_random_line("death.txt", lang=language)
        additional = i18n.get("death.Reason", lang=language, line=line)
        caption += additional
        if filename.endswith('.gif'):
            send_animation(cookiebot, chat_id, fileurl, caption=caption, msg_to_reply=msg)
        else:
            send_photo(cookiebot, chat_id, fileurl, caption=caption, msg_to_reply=msg)

Dispatched `COOKIEBOT.py:216,218-219,238-239`, inside the same `funfunctions`-gated
`elif` chain as `ship.py`/`fun_random.py` — off answers `fun_off`, not silence.
Full contract: `.specs/features/fun_death/spec.md`.

## Why this was blocked, and what unblocked it

v1's image pool was never a file checked into any repo — it was a live listing
of a private GCS bucket, read with 15-minute signed URLs
(`bloblist_death = list(storage_bucket.list_blobs(prefix="Death"))`,
`Miscellaneous.py:17`). `spec.md`'s "The blocker" section is the investigation
that found nothing to vendor. `cb_worker.bucket_export` has since copied that
prefix (and every other v1 prefix) into `cb_core.storage` under
content-addressed keys, and `cb.py legacy-catalog` turns the export manifest
into the small per-prefix catalogs `cb_core.legacy_assets` reads — so this
handler calls `legacy_assets.choose("Death", ...)` rather than vendoring 21.5 MB
of images into the wheel the way `fun_complaint`'s much smaller pool is.

## The gif/photo question, answered from `source_path`

v1 branches on `fileblob.name.endswith('.gif')` — the object's path inside the
bucket. That survives in `LegacyAsset.source_path`, not in `storage_key`
(`LegacyAsset.destination_key`): the content-addressed key's own extension
happens to be copied from the same source name at export time
(`bucket_export.keys.destination_key`), but that is an implementation detail
of the exporter, not a contract this module should lean on — `source_path` is
the field whose *meaning* is "v1's original filename", so `is_gif` reads that
one. The comparison is lower-cased, unlike v1's literal `.endswith('.gif')`:
nothing about a `.GIF` extension's case is a v1-observable behaviour worth
preserving byte for byte, and a case-sensitive check could silently misroute
an upper-case export into `reply_photo`.

## D-DE-1, preserved verbatim

`'💀💀💀 ' + '@'+username if 'username' in msg['from'] else first_name` parses
as `('💀💀💀 @' + username) if has_username else first_name` — a caller with no
Telegram username gets a caption with **no skull-emoji prefix at all**. Kept
exactly (spec.md's verdict): a user-visible quirk of a fun command, the same
category as `fun_ship`'s `@@alice + @@bob`. `resolve_target` below returns the
`skip_skull_prefix` flag that reproduces it, and only for that one branch.

## D-DE-3, fixed

v1 has no empty-pool guard: `random.randint(0, len(bloblist_death)-1)` on an
empty list is `random.randint(0, -1)`, a `ValueError`. `legacy_assets.choose`
returns `None` instead — the same "no bytes seeded yet" shape
`meme_templates.choose` already gives `fun_meme` — and this handler treats
that as "nothing to send" rather than letting an exception reach the
dispatcher. Reachable today, unlike in v1: `legacy-catalog` has not run in
every checkout, so the empty-pool path is a real deployment state, not a
hypothetical. The reaction and chat-action still fire before the pool check
(see `death` below) so the two states look the same to a user either way: a
process that would have crashed after showing 👻 and "uploading photo" now
shows the same two things and then, deliberately, nothing more.

## Telemetry deviation from design.md

`design.md`'s R4.1 called for `mark_outcome("refused")` on the `fun_off` gate
path, mirroring `ship.py`/`complaint.py`. Since that section was written,
`unearth.py` and `fortune.py` (this codebase's newest fun-gated ports) settled
on `deny_if_disabled` without an accompanying `mark_outcome` call — the
gate-refusal reply is already distinguishable on the trace from
`deny_if_disabled`'s own `message.reply` span, so a second manual label was
redundant for this class of handler. This port follows the newer, established
shape rather than the older design note.
"""

from __future__ import annotations

import random
from posixpath import splitext
from typing import cast

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.types import BufferedInputFile, Message, ReactionTypeEmoji

from cb_core import legacy_assets, locales, storage
from cb_core.logging import get_logger
from cb_core.textmatch import ParsedCommand
from cb_gateway.context import context_for, deny_if_disabled
from cb_gateway.filters import CommandName

log = get_logger("cb.gateway.death")

router = Router(name="death")

# Miscellaneous.py:17 — the v1 bucket prefix this pool was exported from, and
# the same string `legacy_assets.entries_for`/`choose` key their catalog on.
_LEGACY_PREFIX = "Death"

# '💀💀💀 ' (Miscellaneous.py:341,343,345) — a module constant rather than a
# literal repeated at both call sites in resolve_target/render_caption.
_SKULL_PREFIX = "💀💀💀 "

# One shared rng for both the asset pick and the caption's random draws — same
# idiom as fun_ship/fun_complaint/fun_fortune's module-level `_rng` (plain
# `random`, not `secrets`: nothing here is security-sensitive).
_rng = random.Random()


# --------------------------------------------------------------------- pure logic


def is_gif(source_path: str) -> bool:
    """v1: `filename.endswith('.gif')` (`Miscellaneous.py:354`) — see the
    module docstring's "The gif/photo question" for why `source_path`, not
    the storage key, is what this reads.
    """
    return source_path.lower().endswith(".gif")


def resolve_target(
    tagged_token: str | None,
    *,
    reply_first_name: str | None,
    sender_username: str | None,
    sender_first_name: str,
) -> tuple[str, bool]:
    """v1's three-branch target resolution (`Miscellaneous.py:341-346`,
    spec.md's target-resolution row). Returns `(target, skip_skull_prefix)`:

    ① `tagged_token` set (v1: `len(msg['text'].split()) > 1`, the raw second
       whitespace token, no membership lookup) ② else `reply_first_name` set
       (the replied-to sender's first name) ③ else the caller's own
       `@username`, or bare first name with no username — the only branch
       where `skip_skull_prefix` is True, reproducing D-DE-1 verbatim (module
       docstring).
    """
    if tagged_token:
        return tagged_token, False
    if reply_first_name is not None:
        return reply_first_name, False
    if sender_username:
        return f"@{sender_username}", False
    return sender_first_name, True


def render_caption(
    target: str, *, skip_skull_prefix: bool, lang: str, rng: random.Random | None = None
) -> str:
    """v1: the caption build (`Miscellaneous.py:345-353`) — target (with or
    without the skull prefix, D-DE-1) + a randomised `death.template` variant
    + a randomised `death.Reason` line. `locales.get_nested` already gives the
    en-fallback and malformed-`%`-placeholder safety `locales.get` gives every
    flat key (`locales.py:180-192`), so no local reimplementation of that
    fallback is needed here — unlike `groupguardian.py`'s `_captcha_strings`,
    written before `get_nested`/`nested_value` existed.
    """
    chooser = rng or random
    variants = locales.nested_value("death", "variants", lang)
    # `nested_value` types its return as `object | None` because a nested
    # catalog entry is not uniformly `str` (`death.variants` is v1's own
    # `list[str]`) — real for every language ported so far, so the `else`
    # branch is a belt for a catalog edit that drops the list, not a path any
    # shipped locale takes today.
    variant = chooser.choice(variants) if isinstance(variants, list) and variants else ""
    template = locales.get_nested("death", "template", lang, variant=variant)
    line = chooser.choice(locales.lines("death", lang))
    reason = locales.get_nested("death", "Reason", lang, line=line)
    prefix = target if skip_skull_prefix else f"{_SKULL_PREFIX}{target}"
    return prefix + template + reason


# --------------------------------------------------------------------- handler


async def _deliver(message: Message, bot: Bot, lang: str, tagged_token: str | None) -> None:
    """Everything past the `fun` gate — reaction, pool pick, target
    resolution, caption, send. Takes `lang`/`tagged_token` directly rather
    than a `ChatContext`/`ParsedCommand` so it needs no database and no admin
    resolution, the same split `fun_random.py`'s `_send_media`/`_pool` make
    between "resolve who's asking" (database-backed, exercised by the QA
    suite) and "given who's asking, what happens" (pure/IO-light, exercised
    here).
    """
    # v1 reacts and signals "sending a photo" before it even knows which blob
    # it will pick (`:336-337`) — best-effort, like every other reaction in
    # this codebase (`ship.py`'s identical `contextlib.suppress`). Kept ahead
    # of the pool check below so an empty pool looks, to the user, exactly
    # like what v1 would have shown right up to the point it would have
    # crashed (module docstring, D-DE-3).
    try:
        await message.react(reaction=[ReactionTypeEmoji(emoji="👻")])
    except Exception as exc:  # noqa: BLE001 - a missing reaction must not cost the answer
        log.info("death.react_failed", error=str(exc))
    await bot.send_chat_action(message.chat.id, "upload_photo")

    entry = legacy_assets.choose(_LEGACY_PREFIX, _rng)
    if entry is None:
        # D-DE-3, fixed: a catalog nobody has generated yet in this
        # deployment, not a crash (module docstring).
        log.warning("death.pool_empty", prefix=_LEGACY_PREFIX)
        return

    reply = message.reply_to_message
    reply_first_name = (
        reply.from_user.first_name if reply is not None and reply.from_user is not None else None
    )
    sender = message.from_user
    target, skip_skull_prefix = resolve_target(
        tagged_token,
        reply_first_name=reply_first_name,
        sender_username=sender.username if sender is not None else None,
        sender_first_name=sender.first_name if sender is not None else "",
    )
    caption = render_caption(target, skip_skull_prefix=skip_skull_prefix, lang=lang, rng=_rng)

    data = await storage.store().get(entry.storage_key)
    _, ext = splitext(entry.source_path)
    file = BufferedInputFile(data, filename=f"death{ext}")
    if is_gif(entry.source_path):
        await message.reply_animation(file, caption=caption)
    else:
        await message.reply_photo(file, caption=caption)


@router.message(F.chat.type != ChatType.PRIVATE, CommandName("death"))
async def death(message: Message, parsed: ParsedCommand | None = None) -> None:
    """`/death`, `/morte`, `/muerte` — all three already map to the `death`
    canonical name in `cb_core/textmatch.py:COMMAND_ALIASES`."""
    if parsed is None:  # pragma: no cover - CommandName always injects a match
        return

    bot = cast(Bot, message.bot)
    ctx = await context_for(bot, message)
    if await deny_if_disabled(message, ctx, "fun"):
        return

    tokens = parsed.args.split()
    await _deliver(message, bot, ctx.lang, tokens[0] if tokens else None)


__all__ = ["death", "is_gif", "render_caption", "resolve_target", "router"]
