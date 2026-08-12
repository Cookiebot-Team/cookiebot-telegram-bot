"""x_sticker_autoreply — builds a global sticker pool from sfw-group stickers,
then replies with one at random to anyone who replies to the bot with a
sticker, a document or an animation.

v1 is two functions, both in `SocialContent.py`, plus three dispatch sites:

- **Write**, `add_to_sticker_database` (`SocialContent.py:208-218`)::

      def add_to_sticker_database(msg):
          BANNED_EMOJIS = ['🍆', '🍑', ... 39 more]
          BANNED_TITLESUBSTRINGS = ['yiff', 'porn', '18+', '+18', 'nsfw',
                                     'hentai', 'rule34', 'r34', 'nude', '🔞']
          if any(x in msg['chat']['title'].lower() for x in BANNED_TITLESUBSTRINGS):
              return
          if (not 'emoji' in msg['sticker']) or any(x in msg['sticker']['emoji'] for x in BANNED_EMOJIS):
              return
          if (not 'set_name' in msg['sticker']) or (not re.match(r'^[a-zA-Z0-9]+$', msg['sticker']['set_name'])):
              return
          stickerId = msg['sticker']['file_id']
          post_request_backend('stickerdatabase', {'id': stickerId})

  Called from the dispatcher's sticker branch, `COOKIEBOT.py:179-182`::

      elif content_type == "sticker":
          sticker_anti_spam(cookiebot, msg, chat_id, stickerspamlimit, language)
          if sfw and 'username' in msg['from']:
              add_to_sticker_database(msg)
          if funfunctions and 'reply_to_message' in msg and msg['reply_to_message']['from']['first_name'] == 'Cookiebot':
              reply_sticker(cookiebot, msg, chat_id)

  Note what that `if` does *not* check: pooling is gated on `sfw` and the
  sender having a `username` — never on `funfunctions`. A group with fun
  features off still feeds this pool; only the read side (below) is
  fun-gated. Preserved exactly, not "fixed" — turning fun off is not the
  same signal as turning sfw off, and starving the pool would only ever
  hurt *other* groups' reads, never this one's.

- **Read**, `reply_sticker` (`SocialContent.py:220-222`)::

      def reply_sticker(cookiebot, msg, chat_id):
          sticker = get_request_backend("stickerdatabase")
          cookiebot.sendSticker(chat_id, sticker['id'], reply_to_message_id=msg['message_id'])

  Dispatched for three content types, all gated on `funfunctions` and a
  reply whose sender's `first_name == 'Cookiebot'`:
  `COOKIEBOT.py:176-178` (document), `:183-184` (sticker — same branch as the
  write side above), and the `animation` branch between them (`:181-182` in
  the source, reproduced verbatim in both branches).

## Deviation 1 — identity check, not a literal name

v1's `first_name == 'Cookiebot'` is wrong for every persona this codebase
ships that is not literally named Cookiebot (`bombot`, `pawstralbot`, ...) —
their own replies never trigger their own read side. v2 already has the
correct check elsewhere (`chat_ai.py`'s `_bot_reply_text`:
`reply.from_user.id != bot.id`) — `_is_reply_to_bot` below does the same
thing, against whichever bot identity is actually answering this update.

## Deviation 2 — see `sticker_pool` table decision in migration `0009`

Full reasoning for choosing a **global reference table** over `fun_random`'s
per-group model — and the write-path cost tradeoff for its `ON CONFLICT DO
NOTHING` plus the Valkey dedupe check in front of it — lives in
`packages/cb-api/migrations/versions/0009_sticker_pool.py`'s module
docstring; not repeated here.

## Router-ordering note (deviation from v1's own in-process order)

`stickerspam.router`'s own handler never raises `SkipHandler` (see its
docstring), so a router registered *after* it in `handlers/__init__.py` never
sees a sticker update at all — aiogram stops propagating the instant a
handler completes without it. `router` below (the sticker branch) must
therefore be registered *before* `stickerspam.router`, and it always raises
`SkipHandler` itself so `core_stickerspam`'s flood counter still runs. That
is the opposite of v1's own in-process order — antispam first, then pool,
then reply, all in one function body — but the three are independent side
effects even in v1: a sticker `sticker_anti_spam` decides to delete for
flooding was already pooled and already replied to by the time the delete
call fires, since nothing in `add_to_sticker_database`/`reply_sticker`
depends on antispam's outcome. Reordering which one "sees" the update first
therefore has no observable effect.

`reply_router` (the document/animation branch) is registered *after*
`postgetter.router` for a different, v1-faithful reason: v1's dispatch is one
big `if/elif` chain where the auto-forwarded-ad check (`ask_publisher`) is
evaluated *before* the document/animation branches
(`COOKIEBOT.py:165-166,174,181`) — a message that qualifies as an ad never
reaches `reply_sticker` in v1 either. `postgetter`'s own handler only stops
propagation when it actually prompts (its own docstring), so every other
document/animation still reaches `reply_router` behind it.
"""

from __future__ import annotations

import re
from typing import Final

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.enums import ChatType
from aiogram.types import Message

from cb_core import cache, db
from cb_core.group_config import GroupConfig
from cb_core.logging import get_logger
from cb_gateway.context import context_for

log = get_logger("cb.gateway.sticker_autoreply")

router = Router(name="sticker_autoreply")
reply_router = Router(name="sticker_autoreply_reply")

# SocialContent.py:194,211 (add_to_random_database, add_to_sticker_database
# share this exact list — fun_random.py's own docstring already names this
# module as the second user of it) — a group whose title flags itself as
# NSFW never feeds the pool.
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

# SocialContent.py:210 — v1's own `BANNED_EMOJIS`, transcribed verbatim (order,
# and duplication of intent — e.g. every skin-tone `👌` variant — kept exactly
# as the source lists them). Literal characters, not `\U...` escapes: this
# repo already writes emoji literally at call sites (analysis.py's `🤔`
# reaction, fun_random.py's `🔞` title marker), and forty-two hand-typed
# escapes is a worse place to introduce a transcription error than one
# `ast.literal_eval` of the v1 source line used to generate this tuple.
_BANNED_EMOJIS: Final[tuple[str, ...]] = (
    "🍆",
    "🍑",
    "🍌",
    "🍭",
    "🥵",
    "💦",
    "🫦",
    "👄",
    "🔞",
    "😏",
    "🇩🇪",
    "⚡",
    "👌",
    "👌🏻",
    "👌🏼",
    "👌🏽",
    "👌🏾",
    "👌🏿",
    "❤️‍🔥",
    "🌽",
    "🍩",
    "🍼",
    "🥛",
    "😫",
    "😩",
    "🌚",
    "♋️",
    "🩸",
    "🪢",
    "👅",
    "😈",
    "🩲",
    "💋",
    "🤤",
    "🍒",
    "🥖",
    "🌶️",
    "💄",
    "🔩",
    "🐙",
    "❤️",
    "🔥",
)

# v1: `re.match(r'^[a-zA-Z0-9]+$', ...)` (SocialContent.py:216).
_SET_NAME_RE = re.compile(r"^[a-zA-Z0-9]+$")

_CACHE_KEY_PREFIX = "cb:stickerpool:seen:"
# How long a cache hit suppresses the real (2PC, replicated) write — see
# migration 0009's docstring, "write-path cost". Not a correctness bound:
# on expiry, or on a cache miss for any other reason, the write still goes
# through `ON CONFLICT DO NOTHING`, so a value here only trades "how much
# redundant Postgres traffic a popular sticker pack causes" against "how
# long a truly new file_id might wait to be visible" — and a new file_id is
# never delayed at all, only a *repeat* of one already pooled.
_DEDUPE_TTL_S = 24 * 60 * 60


# --------------------------------------------------------------------- pure logic


def _has_nsfw_title(title: str | None) -> bool:
    """`SocialContent.py:194,211`: `any(x in msg['chat']['title'].lower() for x in [...])`."""
    if not title:
        return False
    lowered = title.lower()
    return any(marker in lowered for marker in _NSFW_TITLE_SUBSTRINGS)


def _is_banned_emoji(emoji: str) -> bool:
    """`any(x in msg['sticker']['emoji'] for x in BANNED_EMOJIS)` — v1 checks
    substring containment, not equality, against the sticker's own `emoji`
    field (`SocialContent.py:215`). Ported literally: a sticker whose single
    `emoji` field happens to equal one of these still matches, since a string
    is always a substring of itself."""
    return any(banned in emoji for banned in _BANNED_EMOJIS)


def _valid_set_name(set_name: str) -> bool:
    """`re.match(r'^[a-zA-Z0-9]+$', msg['sticker']['set_name'])` (`:216`)."""
    return _SET_NAME_RE.match(set_name) is not None


def _should_pool_sticker(
    config: GroupConfig,
    *,
    has_username: bool,
    chat_title: str | None,
    emoji: str | None,
    set_name: str | None,
) -> bool:
    """`if sfw and 'username' in msg['from']: add_to_sticker_database(msg)`
    (`COOKIEBOT.py:180`) plus `add_to_sticker_database`'s own three guards
    (`SocialContent.py:210-216`). No `funfunctions` check — see the module
    docstring's "Deviation" section header, which is not a deviation but a
    call-out of a real v1 asymmetry this port preserves.
    """
    if not (config.sfw and has_username):
        return False
    if _has_nsfw_title(chat_title):
        return False
    if emoji is None or _is_banned_emoji(emoji):
        return False
    return set_name is not None and _valid_set_name(set_name)


def _is_reply_to_bot(message: Message, bot_id: int) -> bool:
    """v1: `msg['reply_to_message']['from']['first_name'] == 'Cookiebot'`
    (`COOKIEBOT.py:174,177,183`) — replaced with an identity check against
    the bot actually answering this update (module docstring, deviation 1),
    the same seam `chat_ai.py`'s `_bot_reply_text` already established."""
    reply = message.reply_to_message
    return reply is not None and reply.from_user is not None and reply.from_user.id == bot_id


# --------------------------------------------------------------------------- I/O


def _cache_key(file_id: str) -> str:
    return f"{_CACHE_KEY_PREFIX}{file_id}"


async def _recently_pooled(file_id: str) -> bool:
    """True when this exact `file_id` was pooled (or attempted) within
    `_DEDUPE_TTL_S`. Fails open to `False` on a Valkey outage — the caller's
    fallback is the real `ON CONFLICT DO NOTHING` write, which is always
    correct, just more expensive; see migration 0009's docstring."""
    try:
        was_new = await cache.client().set(_cache_key(file_id), b"1", nx=True, ex=_DEDUPE_TTL_S)
    except Exception as exc:  # noqa: BLE001 - infra outage must fail open, not raise
        log.warning("sticker_autoreply.cache_failed", error=str(exc))
        return False
    return not was_new


async def _pool_sticker(file_id: str) -> None:
    """`add_to_sticker_database`'s persistence half: `post_request_backend
    ('stickerdatabase', {'id': stickerId})` (`SocialContent.py:217-218`) ->
    an idempotent upsert into the global `sticker_pool` reference table
    (migration 0009)."""
    if await _recently_pooled(file_id):
        return
    await db.execute(
        "INSERT INTO sticker_pool (file_id) VALUES ($1) ON CONFLICT (file_id) DO NOTHING",
        file_id,
        name="sticker_pool_add",
    )


async def _random_sticker() -> str | None:
    """`reply_sticker`'s read half: `get_request_backend("stickerdatabase")`
    (`SocialContent.py:221`), which called `StickerDatabaseService.getRandom`
    — Mongo's own `$sample` aggregation, one document, or `null` on an empty
    collection. `ORDER BY random() LIMIT 1` is the same "one row, no scan
    application-side" shape against a table this small."""
    row = await db.fetchrow(
        "SELECT file_id FROM sticker_pool ORDER BY random() LIMIT 1", name="sticker_pool_random"
    )
    return row["file_id"] if row is not None else None


async def _reply_with_sticker(message: Message) -> None:
    file_id = await _random_sticker()
    if file_id is None:
        # v1 parity: `StickerDatabaseService.getRandom()` returns `null` on an
        # empty pool, and `reply_sticker`'s `sendSticker(..., sticker=None)`
        # blows up inside telepot — an exception v1's own outer dispatcher
        # swallows, so the observable behaviour is "the bot says nothing."
        # Reproduced as an explicit no-op, not a caught exception racing to
        # the same place.
        return
    await message.reply_sticker(file_id)


# --------------------------------------------------------------------- handlers


@router.message(F.chat.type != ChatType.PRIVATE, F.sticker)
async def sticker_update(message: Message, bot: Bot) -> None:
    """v1's `elif content_type == "sticker":` branch minus `sticker_anti_spam`
    itself, which stays exactly where it is in `stickerspam.py` (module
    docstring: "must not fight it"). Always raises `SkipHandler` — see the
    module docstring's router-ordering note for why that is required, not
    optional, here.
    """
    try:
        ctx = await context_for(bot, message)
        sticker = message.sticker
        if sticker is not None:
            user = message.from_user
            if _should_pool_sticker(
                ctx.config,
                has_username=user is not None and user.username is not None,
                chat_title=message.chat.title,
                emoji=sticker.emoji,
                set_name=sticker.set_name,
            ):
                await _pool_sticker(sticker.file_id)

        if ctx.enabled("fun") and _is_reply_to_bot(message, bot.id):
            await _reply_with_sticker(message)
    except Exception as exc:  # noqa: BLE001 - a pooling/reply failure must never break stickerspam downstream
        log.warning("sticker_autoreply.sticker_failed", error=str(exc))
    raise SkipHandler


@reply_router.message(F.chat.type != ChatType.PRIVATE, F.document | F.animation)
async def reply_to_document_or_animation(message: Message, bot: Bot) -> None:
    """v1's `elif content_type == "document"` / `"animation"` branches
    (`COOKIEBOT.py:174-178,181-182`): reply-to-bot only, no pooling — v1
    never calls `add_to_sticker_database` for anything but a real sticker.

    Checks `_is_reply_to_bot` (pure, no I/O) before `context_for` (a
    cache/DB round trip) on purpose: almost every document/animation in a
    group is not a reply to the bot, and there is no reason to pay for a
    config lookup on each one just to find that out.
    """
    if _is_reply_to_bot(message, bot.id):
        try:
            ctx = await context_for(bot, message)
            if ctx.enabled("fun"):
                await _reply_with_sticker(message)
        except Exception as exc:  # noqa: BLE001 - see sticker_update
            log.warning("sticker_autoreply.doc_anim_failed", error=str(exc))
    raise SkipHandler


__all__ = [
    "reply_router",
    "reply_to_document_or_animation",
    "router",
    "sticker_update",
]
