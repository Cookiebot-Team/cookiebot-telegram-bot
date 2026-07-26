"""core_mediarestrict — media restriction for new members.

v1: `welcome_message` (`../COOKIEBOT-Telegram-Group-Bot/Bot/GroupShield.py:140-152`,
called from the join event at `COOKIEBOT.py:141,150`) calls `restrictChatMember`
twice — grant full permissions, then immediately re-restrict with
`can_send_media_messages=False, can_send_other_messages=False,
can_add_web_page_previews=False` and an `until_date` `limbotimespan` seconds in
the future — then sends `i18n.get("restrict_message", time=round(limbotimespan/60))`.
This only runs `if limbotimespan > 0` (`GroupShield.py:145`); `limbotimespan == 0`
is v1's "off" switch — no restriction call, no message, ever. `limbotimespan` is
`configs.timeWithoutSendingImages`, ported as `ctx.config.media_restrict_seconds`
(default 600s = 10 minutes; the message text always shows `round(x/60)` minutes
of the *configured* window, not remaining time — v1 computes this once, at join
time, and never again).

FEATURE-MAP's `core_mediarestrict` row also cites `COOKIEBOT.py:167-172` (the
dispatcher's photo/video branch) as "the trigger". Read literally, that code is
`add_to_random_database` — `fun_random`'s feature, unrelated to restriction.
Its real significance here is structural, not textual: a *restricted* member's
photo/video message never reaches that branch, or any other line of Python, at
all. Telegram's own client refuses to let a muted-for-media user attach a
photo, and the server never delivers the message to the bot. v1's enforcement
is 100% native and preventive; there is no bot-side check anywhere in v1 for
this feature, only the `restrictChatMember` call and the message that follows
it in `welcome_message`.

## v1 vs v2 mechanism (the re-architecture) — full comparison in
docs/contracts/core_mediarestrict.md. Summary: v1 mutes natively at join time
with an `until_date`; v2 has no persistent notion of "when did this member
join" in v1 at all, so a failed/never-delivered `restrictChatMember` call
(`GroupShield.py:151-152` only `print()`s on exception) permanently and
silently un-restricts that member with no record anywhere. v2 instead records
`joined_at` in `group_members` (migration 0001's own comment: "has this member
been here longer than the limit?") when a member joins, and on every
subsequent message carrying a restricted content type, compares
`now() - joined_at` against `ctx.config.media_restrict_seconds`. Still inside
the window -> the bot deletes the message after the fact and replies with the
same `restrict_message` text v1 showed at join time. This is reactive
(post-hoc delete), not preventive (native block) — see the contract doc for
why that is an intentional, observable difference, not a bug.

## Router-ordering caveat (flagged for the wiring owner, not fixed here)

This module registers its own `@router.message(F.new_chat_members)` handler to
write the `group_members` row (`core_welcome`'s join handler explicitly does
not touch this table — see `docs/contracts/core_welcome.md`'s Boundary
section). `welcome.router` has an unconditional handler on the exact same
filter that never raises `SkipHandler`, and aiogram's `Router.trigger()` stops
propagating to sibling routers the instant one handler completes without it
(verified against the installed aiogram source, `Router._propagate_event` /
`TelegramEventObserver.trigger`) — so whichever of `welcome.router` /
`mediarestrict.router` ends up registered first in `handlers/__init__.py` (not
owned by this task) silently prevents the other's join handler from running
for a given update. `core_welcome`'s own contract already flagged this exact
class of problem for captcha/doomlist and left it to the wiring owner; this
port does the same, and additionally makes the enforcement handler tolerant of
losing that race (see "no group_members row" below), so a lost race degrades
to "not yet restricted" rather than an exception.

Persistence: `group_members` (`packages/cb-api/migrations/versions/
0001_initial_schema.py:230-248`), distributed on `group_id`, colocated with
`groups`. This feature only ever reads/writes `group_id`, `user_id`,
`joined_at`, `left_at` — `message_count` belongs to another feature.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from typing import cast

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.types import Message

from cb_core import db
from cb_core.logging import get_logger
from cb_gateway.context import context_for, t

log = get_logger("cb.mediarestrict")

router = Router(name="mediarestrict")

# The union of v1's two restriction groups (GroupShield.py:148):
#   can_send_media_messages   -> photo, video, document, audio, voice, video_note
#   can_send_other_messages   -> animation, sticker (games are not reachable
#                                 through this codebase's handlers)
# `can_add_web_page_previews` (a link preview riding on an ordinary text
# message) has no clean reactive equivalent: deleting a whole text message
# because it happens to carry a preview is a materially bigger, more visible
# behaviour change than deleting a photo, and is out of scope here.
_RESTRICTED_CONTENT = (
    F.photo | F.video | F.animation | F.sticker | F.voice | F.video_note | F.document | F.audio
)


# --------------------------------------------------------------------- pure logic


def _is_within_restriction_window(
    joined_at: datetime, media_restrict_seconds: int, *, now: datetime | None = None
) -> bool:
    """GroupShield.py:145: `if limbotimespan > 0`. A configured window of zero
    (or, defensively, negative) means the feature is off — v1 never restricts
    in that case, so neither does this."""
    if media_restrict_seconds <= 0:
        return False
    reference = now if now is not None else datetime.now(UTC)
    elapsed = (reference - joined_at).total_seconds()
    return elapsed < media_restrict_seconds


def _restrict_minutes(media_restrict_seconds: int) -> int:
    """GroupShield.py:149: `round(limbotimespan/60)` — the *configured* window,
    not remaining time. v1 computes this once, at join time; this port shows
    the same number on every blocked attempt, which is the honest v2
    equivalent (see the contract doc's mechanism comparison)."""
    return round(media_restrict_seconds / 60)


# --------------------------------------------------------------------------- db


async def _record_join(group_id: int, user_id: int) -> None:
    """Single-shard insert, filtered on `group_id` (AGENTS.md §4).

    `ON CONFLICT DO NOTHING`: a rejoin does not reset the clock here — no
    handler in this codebase manages `left_at` / rejoin lifecycle yet, so
    overwriting `joined_at` on every join risks fighting a future owner of
    that column rather than fixing anything real today.
    """
    await db.execute(
        """
        INSERT INTO group_members (group_id, user_id)
        VALUES ($1, $2)
        ON CONFLICT (group_id, user_id) DO NOTHING
        """,
        group_id,
        user_id,
        name="mediarestrict_record_join",
    )


async def _joined_at(group_id: int, user_id: int) -> datetime | None:
    """Single-shard read, same predicate shape as `group_members_joined_idx`
    (migration 0001: `group_id` first, `left_at IS NULL`)."""
    row = await db.fetchrow(
        """
        SELECT joined_at FROM group_members
        WHERE group_id = $1 AND user_id = $2 AND left_at IS NULL
        """,
        group_id,
        user_id,
        name="mediarestrict_joined_at",
    )
    return row["joined_at"] if row is not None else None


# --------------------------------------------------------------------- handlers


@router.message(F.new_chat_members)
async def record_join(message: Message) -> None:
    """Records the moment this member can start being un-restricted from.

    v1 quirk, preserved for parity: `welcome_message`'s `restrictChatMember`
    call reads only the deprecated singular `new_chat_member` field (==
    `new_chat_members[0]`) — see `docs/contracts/core_welcome.md`'s identical
    note about the *messaging* half of the same v1 function. The 2nd+ joiner
    in a batch join was never natively restricted in v1 either, so this port
    does not record their join time — they fall through the "no
    `group_members` row" fail-open path below, which reproduces the same
    "never restricted" outcome v1 actually had for them.

    This is bookkeeping, not a reply: it records and then always raises
    `SkipHandler`, so the doomlist, the captcha and the welcome still see the
    same join. Recording first — even for a member the doomlist is about to ban
    — costs one row and keeps "when did this member arrive?" true regardless of
    which branch of the join chain runs.
    """
    joiners = message.new_chat_members
    if not joiners:
        raise SkipHandler
    newcomer = joiners[0]
    bot = cast(Bot, message.bot)
    if not (newcomer.is_bot or newcomer.id == bot.id):
        try:
            await _record_join(message.chat.id, newcomer.id)
        except Exception as exc:  # noqa: BLE001 - see below
            # This handler runs first in the join chain, so an exception here
            # takes the doomlist ban, the captcha and the welcome down with it —
            # a database blip would turn every join into silence, which is worse
            # than losing one member's restriction window. Log and carry on; the
            # enforcement path already fails open on a missing row.
            log.warning(
                "mediarestrict.record_join_failed",
                group_id=message.chat.id,
                user_id=newcomer.id,
                error=str(exc),
            )
    raise SkipHandler


@router.message(_RESTRICTED_CONTENT)
async def enforce_media_restriction(message: Message) -> None:
    """Every path that does not act raises `SkipHandler`.

    Restricted content overlaps other features — a sticker is both "media from a
    new member" and "one more sticker in the flood counter". aiogram stops at the
    first router that *handles* an update, so returning quietly here would make
    this handler swallow every sticker in the group before
    `core_stickerspam` ever saw it. `SkipHandler` means "not mine", and the
    dispatcher carries on to the next router.
    """
    user = message.from_user
    if user is None:
        raise SkipHandler

    bot = cast(Bot, message.bot)
    ctx = await context_for(bot, message)
    if ctx.config.media_restrict_seconds <= 0:
        raise SkipHandler
    if ctx.is_admin:
        raise SkipHandler

    joined_at = await _joined_at(ctx.group_id, user.id)
    if joined_at is None:
        # Never recorded joining: a lost race with the router-ordering
        # caveat above, a member who joined before this feature existed, or a
        # bot restart that missed the join event. Fail open — an unknown join
        # time must never turn into restricting an arbitrary existing member
        # forever, which is a worse failure mode than under-enforcing once.
        raise SkipHandler
    if not _is_within_restriction_window(joined_at, ctx.config.media_restrict_seconds):
        raise SkipHandler

    # Reactive, not preventive (see the module docstring): the message already
    # exists, so the best this port can do is remove it and warn, best-effort
    # — v1 never needed delete rights for this feature at all.
    with contextlib.suppress(Exception):
        await bot.delete_message(message.chat.id, message.message_id)

    await bot.send_message(
        message.chat.id,
        t(ctx, "restrict_message", time=_restrict_minutes(ctx.config.media_restrict_seconds)),
    )
