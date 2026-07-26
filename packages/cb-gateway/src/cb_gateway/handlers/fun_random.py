"""fun_random — `/random` (`/aleatorio`) plus the pooling that feeds it.

v1 is two separate pieces of code, both in `SocialContent.py`:

- The read side, `random_media` (`SocialContent.py:198-206`): up to 50 attempts
  of `GET randomdatabase` (a *global*, cross-group pool the Java backend picked
  one row from by loading the **entire** collection into the JVM,
  `RandomDatabaseService.getRandom`) followed by a native Telegram
  `forwardMessage` of whatever `{id: chat_id, idMessage: message_id}` came back,
  into `thread_id` if the trigger was inside a topic. Any exception (empty
  result, the source message having since been deleted, the source chat having
  banned this bot, ...) is caught and the loop just tries again; if all 50
  attempts fail, `random_media` returns having sent nothing at all — a silent,
  observable-as-nothing failure mode, preserved below.
- The write side, `add_to_random_database` (`SocialContent.py:191-196`), called
  from the dispatcher's photo/video branches only when
  `sfw and funfunctions and not publisherpost` (`COOKIEBOT.py:168-172` — also
  the exact line range `docs/contracts/core_mediarestrict.md` cites and
  disclaims as "this is fun_random's code, not a restriction check"). It skips
  forwarded messages (`'forward_from' in msg or 'forward_from_chat' in msg`)
  and groups whose title contains an NSFW-flagging substring, then remembers
  only `{chat_id, message_id, photo_file_id}` — no bytes are ever downloaded in
  v1; the "database" is a pointer to a still-live message, and `random_media`
  forwards that live message later.

Dispatch: `COOKIEBOT.py:213-220`. `/random`/`/aleatorio`/`/aleatório` share one
`elif` arm with a dozen unrelated fun commands, gated as a block:
`if not funfunctions: notify_fun_off(cookiebot, msg, chat_id, language)` —
`Miscellaneous.py:129-131`, text key `"fun_off"` — `elif` `/aleatorio`/
`/aleatório`/`/random` specifically: `random_media(...)`. This is *not* the
generic "gated commands answer nothing" shape `cb_gateway.filters.FeatureGate`
implements (see that filter's own docstring) — this command family explicitly
answers with the `fun_off` text, so this module checks `ctx.enabled("fun")`
itself rather than filtering the update away silently.

## The re-architecture (media.py already committed to this; ported here)

`cb_core.storage.media.MediaService`'s own docstring already explains why: v1's
pool is one unbounded global Mongo collection with no dedupe, read by loading
every row into application memory to pick one — the exact anti-pattern Citus
punishes hardest (FEATURE-MAP's own note: "backend loads whole collection to
pick 1"). v2 makes the pool **per-group** (`media_objects.group_id`, the
distribution column) and **content-addressed** (`media_objects.content_hash`,
deduped across groups at the blob layer) — `/random` becomes a single-shard
`ORDER BY random() LIMIT 1`, not a JVM-side full scan, and this port's job is
only to feed that table and read it back, not to change its shape.

Two consequences worth being explicit about, since they are real, observable
differences from v1 and not merely different code for the same behaviour:

1. **Scope**: v1's pool is global — `/random` in group A can return media
   originally posted in group B. v2's is per-group by construction (Citus
   colocation, AGENTS.md §4) — `/random` only ever returns something *this*
   group has itself posted. A brand-new group's pool is empty and stays empty
   until its own members post photos/videos.
2. **Delivery**: v1 `forwardMessage`s the original, live message (so Telegram
   shows "Forwarded from ..." and the original caption). v2 re-sends the
   stored bytes (or, when available, replays the original `file_id` — no
   re-upload, no caption). `media_objects` has no caption column and this port
   does not add one (out of this task's file-ownership boundary — see
   `docs/contracts/fun_random.md`), so a caption on the original post never
   reaches the resend. Flagged, not silently dropped.

## Kinds pooled

Only `"photo"` and `"video"` — the exact two v1 branches
(`COOKIEBOT.py:168-172`) ever call `add_to_random_database` from. `"animation"`
is deliberately not written here even though `MediaService.random`'s default
`kinds` includes it (that default is shared with other future callers of the
same table); v1's animated-GIF/sticker reply path (`reply_sticker`, triggered
by replying to the bot) is a different feature and a different table
(`add_to_sticker_database`), out of scope for this port.
"""

from __future__ import annotations

from typing import Final

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.enums import ChatType
from aiogram.types import BufferedInputFile, Message

from cb_core import storage
from cb_core.group_config import GroupConfig
from cb_core.logging import get_logger
from cb_core.storage import MediaRef
from cb_core.storage.keys import extension_for
from cb_gateway.context import ChatContext, context_for, t
from cb_gateway.filters import CommandName

log = get_logger("cb.gateway.fun_random")

router = Router(name="fun_random")

# SocialContent.py:194,211 (add_to_random_database, add_to_sticker_database
# share this exact list) — a group whose title flags itself as NSFW never
# feeds the pool, regardless of the sfw config flag.
_NSFW_TITLE_SUBSTRINGS: Final[tuple[str, ...]] = (
    "yiff",
    "porn",
    "18+",
    "+18",
    "nsfw",
    "hentai",
    "rule34",
    "r34",
    "nude",
    "\U0001f51e",  # 🔞
)

# The only two kinds v1's write side ever produces (see module docstring).
_POOLED_KINDS: Final[tuple[str, ...]] = ("photo", "video")


# --------------------------------------------------------------------- pure logic


def _has_nsfw_title(title: str | None) -> bool:
    """`SocialContent.py:194`: `any(x in msg['chat']['title'].lower() for x in [...])`."""
    if not title:
        return False
    lowered = title.lower()
    return any(marker in lowered for marker in _NSFW_TITLE_SUBSTRINGS)


def _is_forwarded(message: Message) -> bool:
    """`'forward_from' in msg or 'forward_from_chat' in msg` (`SocialContent.py:192`).

    Bot API 7.0 replaced both fields with the single `forward_origin` union;
    aiogram's `Message` only ever populates the new field, so this is the
    faithful equivalent, not a guess.
    """
    return message.forward_origin is not None


def _pool_kind_and_file_id(message: Message) -> tuple[str, str] | None:
    """`COOKIEBOT.py:168-172`: photo takes the largest size, video the object
    itself. Anything else (document, sticker, animation, ...) is not pooled."""
    if message.photo:
        return "photo", message.photo[-1].file_id
    if message.video:
        return "video", message.video.file_id
    return None


def _should_pool(config: GroupConfig, *, chat_title: str | None, forwarded: bool) -> bool:
    """`if sfw and funfunctions and not publisherpost:` (`COOKIEBOT.py:169,171`)
    plus `add_to_random_database`'s own two guards (`SocialContent.py:192,194`).

    `sfw` gates the *write* side in v1 — the pool only ever accumulates content
    from groups configured safe-for-work in the first place, which is what lets
    the read side (`random_media`) forward without checking the target group's
    own `sfw` flag at all: everything in the pool is already known-safe. This
    port keeps that write-side gate and additionally honours `sfw` again on the
    read side (`_select_kinds`/`send_random_media` below) for a group that
    later flips the flag off.
    """
    if not (config.sfw and config.functions_fun and not config.publisher_post):
        return False
    if forwarded:
        return False
    return not _has_nsfw_title(chat_title)


def _content_type_for(kind: str, message: Message) -> str | None:
    if kind == "photo":
        return "image/jpeg"  # Telegram always transcodes photos to JPEG.
    if kind == "video" and message.video is not None:
        return message.video.mime_type
    return None


# --------------------------------------------------------------------------- I/O


async def _download(bot: Bot, file_id: str) -> bytes | None:
    """The one network call this port adds over v1 (see module docstring:
    v1 never downloads bytes, only remembers a pointer). Any failure — the
    bot lacking file access, a timeout, the mock/test Telegram API not
    implementing `getFile` — must never take the update down with it, so this
    is the single seam callers wrap nothing further around.
    """
    try:
        buffer = await bot.download(file_id)
    except Exception as exc:  # noqa: BLE001 - downloading is best-effort, see above
        log.warning("fun_random.download_failed", error=str(exc))
        return None
    if buffer is None:
        return None
    return buffer.read()


async def _pool(
    bot: Bot,
    message: Message,
    group_id: int,
    kind: str,
    file_id: str,
    *,
    uploaded_by: int | None,
) -> None:
    data = await _download(bot, file_id)
    if data is None:
        return
    await storage.media().put(
        group_id,
        kind,
        data,
        uploaded_by=uploaded_by,
        content_type=_content_type_for(kind, message),
        telegram_file_id=file_id,
        # Always True: `_should_pool` already required `config.sfw`, so nothing
        # this handler ever writes is anything but sfw-sourced (see its docstring).
        sfw=True,
    )


async def _select_media(ctx: ChatContext) -> MediaRef | None:
    """`sfw_only=ctx.config.sfw`: the flag applies again on read, for a group
    that has since turned `sfw` off (see `_should_pool`'s docstring)."""
    return await storage.media().random(ctx.group_id, kinds=_POOLED_KINDS, sfw_only=ctx.config.sfw)


async def _send_media(message: Message, ref: MediaRef) -> None:
    """Prefers the stored `telegram_file_id` (no re-upload, Telegram just
    re-serves its own cached copy) and falls back to the stored bytes only
    when no file id was ever recorded."""
    file: str | BufferedInputFile
    if ref.telegram_file_id is not None:
        file = ref.telegram_file_id
    else:
        data = await storage.media().get_bytes(ref)
        file = BufferedInputFile(data, filename=f"random{extension_for(ref.kind)}")

    if ref.kind == "photo":
        await message.answer_photo(file)
    elif ref.kind == "video":
        await message.answer_video(file)
    else:  # pragma: no cover - defensive; _POOLED_KINDS never writes anything else
        log.warning("fun_random.unexpected_kind", kind=ref.kind)


# --------------------------------------------------------------------- handlers


@router.message(F.chat.type != ChatType.PRIVATE, F.photo | F.video)
async def pool_media(message: Message, bot: Bot) -> None:
    """Bookkeeping only, never a reply: always raises `SkipHandler` so
    `core_mediarestrict`'s own photo/video handler (and any other router with
    an interest in the same update) still gets to run — see
    `docs/contracts/core_mediarestrict.md`'s router-ordering caveat, which
    applies here in the opposite direction (this handler must not be the one
    that silently eats everyone else's photo messages).
    """
    kind_and_file = _pool_kind_and_file_id(message)
    if kind_and_file is not None:
        kind, file_id = kind_and_file
        try:
            ctx = await context_for(bot, message)
            if _should_pool(
                ctx.config, chat_title=message.chat.title, forwarded=_is_forwarded(message)
            ):
                uploaded_by = message.from_user.id if message.from_user else None
                await _pool(bot, message, ctx.group_id, kind, file_id, uploaded_by=uploaded_by)
        except Exception as exc:  # noqa: BLE001 - a pooling failure must never break the reply path
            log.warning("fun_random.pool_failed", kind=kind, error=str(exc))
    raise SkipHandler


@router.message(F.chat.type != ChatType.PRIVATE, CommandName("random"))
async def send_random_media(message: Message, bot: Bot) -> None:
    """`/random` / `/aleatorio` (`/aleatório` too — see
    docs/contracts/fun_random.md's parity table for the alias this port cannot
    add itself, out of file ownership).

    Not gated with `FeatureGate("fun")`: that filter answers nothing when the
    area is off, but v1 explicitly replies with `fun_off` for this exact
    command family (`COOKIEBOT.py:218-219`) — reproduced here instead.
    """
    ctx = await context_for(bot, message)
    if not ctx.enabled("fun"):
        await message.answer(t(ctx, "fun_off"))
        return

    ref = await _select_media(ctx)
    if ref is None:
        # v1 parity: `random_media` gives up silently after 50 failed attempts
        # (`SocialContent.py:198-206`) — an empty/exhausted pool produces no
        # reply at all, not an error message.
        return

    await _send_media(message, ref)
