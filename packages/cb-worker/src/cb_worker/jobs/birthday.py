"""`/birthday`'s collage, and the deferred next-birthdays follow-up that
replaces v1's in-process timer. v1: `birthday`/`make_birthday_collage`/
`make_birthday_caption`, `../COOKIEBOT-Telegram-Group-Bot/Bot/Birthdays.py:14-101`,
manual shape only — see `.specs/features/util_birthday/spec.md` for why the
unattended, every-group cron shape is not built here, and for why that
absence is an open parity gap, not a resolved one.

Photo sourcing follows `fun_battle`'s precedent exactly: `cb_core.members.roster`
resolves a name to a real `user_id`, `bot.get_user_profile_photos` resolves
that to a photo — no `telegram.me` scrape (`get_profile_image`,
`SocialContent.py:280-292`, the same mechanism `fun_battle`'s D-BT-2
replaced). Compositing is `cb_worker.collage` (pure, unit-tested separately);
this module is the I/O around it — fetching photos, falling back to the
vendored placeholder, sending, pinning, scheduling the follow-up.

Deliberately does not import `cb_worker.main`, same reasoning every other
job module in this package gives. Does not import `cb_gateway` either —
a worker importing the gateway package would be the same layering violation
`cb_core/bot.py`'s docstring already warns against; the deferred follow-up
is scheduled through `ctx["redis"]` (arq's own pool, handed to every job by
the worker itself) directly, not through `cb_gateway.queue.enqueue`.
"""

from __future__ import annotations

import io
import time
from datetime import UTC, datetime
from typing import Any

from aiogram import Bot
from aiogram.types import BufferedInputFile
from opentelemetry.trace import SpanKind
from PIL import Image
from prometheus_client import Counter

from cb_core import assets, birthdays, jobs, members
from cb_core.logging import get_logger
from cb_core.metrics import job_duration
from cb_core.telemetry import context_from_carrier, span
from cb_worker.collage import build_collage

log = get_logger("cb.worker.birthday")

# outcome in fetched|placeholder — never a group/user id label (AGENTS.md §7).
birthday_photo_total = Counter(
    "cb_worker_birthday_photo_total", "Photo resolutions for the birthday collage", ["outcome"]
)


def _find_in_roster(roster: tuple[members.MemberRef, ...], raw: str) -> int | None:
    """Case-insensitive username match against the group's own roster —
    `fun_battle`'s `_find_in_roster`, copied not imported (every job module
    in this package stays self-contained, same reasoning `everyone.py`/
    `calladms.py` already give for their own copied wrapper shape)."""
    token = raw.strip().lstrip("@").lower()
    if not token:
        return None
    for member in roster:
        if member.username and member.username.lower() == token:
            return member.user_id
    return None


def _targets(
    people: tuple[birthdays.BirthdayPerson, ...],
    extra_names: list[str],
    roster: tuple[members.MemberRef, ...],
) -> list[tuple[str, int | None]]:
    """v1's own two lists concatenated, no deduplication
    (`Birthdays.py:41-42`: `bd_users_in_group.extend(...)`, appended
    regardless of whether that name is already present from the real
    lookup) — preserved: a tagged person who also has a real birthdate on
    file appears twice, same as v1."""
    targets: list[tuple[str, int | None]] = [
        (birthdays.display_name(person), person.user_id) for person in people
    ]
    for raw in extra_names:
        token = raw.strip().lstrip("@")
        if not token:
            continue
        targets.append((f"@{token}", _find_in_roster(roster, token)))
    return targets


async def _photo_for(bot: Bot, user_id: int | None) -> Image.Image:
    """A resolved `user_id`'s most recent profile photo, or the vendored
    placeholder — v1's own fallback (`Birthdays.py:64-69`,
    `cv2.imread('Static/No_Image_Available.jpg', ...)`), now a package asset
    instead of a relative path assuming the process's cwd."""
    if user_id is not None:
        try:
            photos = await bot.get_user_profile_photos(user_id, limit=1)
            if photos.photos:
                file_id = photos.photos[0][-1].file_id
                buffer = await bot.download(file_id)
                if buffer is not None:
                    birthday_photo_total.labels(outcome="fetched").inc()
                    return Image.open(buffer).convert("RGBA")
        except Exception as exc:  # noqa: BLE001 - a missing photo degrades to the placeholder
            log.warning("birthday.photo_failed", error=str(exc))
    birthday_photo_total.labels(outcome="placeholder").inc()
    return Image.open(assets.path("birthday", "No_Image_Available.jpg")).convert("RGBA")


async def _schedule_followup(ctx: dict[str, Any], *, group_id: int, lang: str) -> None:
    """v1: `threading.Timer(900, next_birthdays, ...)` (`Birthdays.py:56-57`)
    — in-process memory, silently dropped by a restart between the collage
    post and 900 seconds later (D-BD-2). `ctx["redis"]` is the same `arq`
    pool the worker itself runs on (arq hands every job its own pool at
    `ctx["redis"]`); `_defer_by` is arq's native deferred execution, durable
    in Redis. Never raises into the caller — a missed follow-up is not worth
    failing the collage job that already succeeded.
    """
    try:
        await ctx["redis"].enqueue_job(
            jobs.NEXT_BIRTHDAYS_FOLLOWUP, group_id=group_id, lang=lang, _defer_by=900
        )
    except Exception as exc:  # noqa: BLE001 - see docstring
        log.warning("birthday.followup_schedule_failed", group_id=group_id, error=str(exc))


async def post_birthday_collage(
    ctx: dict[str, Any],
    *,
    group_id: int,
    message_id: int,
    extra_names: list[str],
    lang: str,
) -> None:
    parent = context_from_carrier(ctx.get("trace_carrier"))
    start = time.perf_counter()
    outcome = "ok"
    token = None
    try:
        from opentelemetry import context as otel_context

        token = otel_context.attach(parent)
        with span("job.post_birthday_collage", kind=SpanKind.CONSUMER):
            await _post(ctx, group_id, message_id, extra_names, lang)
    except Exception:
        outcome = "error"
        log.exception("job.failed", job="birthday_collage")
        raise
    finally:
        if token is not None:
            from opentelemetry import context as otel_context

            otel_context.detach(token)
        job_duration.labels(job="birthday_collage", outcome=outcome).observe(
            time.perf_counter() - start
        )


async def _post(
    ctx: dict[str, Any], group_id: int, message_id: int, extra_names: list[str], lang: str
) -> None:
    bot: Bot = ctx["bot"]
    today = datetime.now(UTC).date()
    people = await birthdays.members_with_birthday(group_id, today.month, today.day)
    roster = await members.roster(group_id)
    targets = _targets(people, extra_names, roster)

    if not targets:
        # Not one of v1's own named branches -- v1 only ever reaches this
        # function's body when manual_chat_id forces the post regardless of
        # `bd_users_in_group` being empty (`Birthdays.py:44`'s `or
        # manual_chat_id`), so v1 would in fact composite a zero-photo
        # collage here (and crash on `collage_images[0]`, Birthdays.py:73).
        # That crash has no user-visible "behaviour" to preserve; degrading
        # to nothing sent is the honest equivalent of "there was nobody to
        # show," which v1's own D-BD-3-adjacent bug never let anyone see.
        log.info("birthday.no_targets", group_id=group_id)
        return

    images = [await _photo_for(bot, user_id) for _, user_id in targets]
    confetti = Image.open(assets.path("birthday", "Confetti.png"))
    collage = build_collage(images, confetti)
    buffer = io.BytesIO()
    collage.convert("RGB").save(buffer, format="PNG")
    buffer.seek(0)

    names = " e ".join(name for name, _ in targets)
    caption = birthdays.bday_cta(lang, names=names) + birthdays.bday_closing(
        lang, date=today.isoformat()
    )

    sent = await bot.send_photo(
        group_id,
        BufferedInputFile(buffer.read(), filename="birthday.png"),
        caption=caption,
        reply_to_message_id=message_id,
    )
    try:
        await bot.pin_chat_message(group_id, sent.message_id)
    except Exception as exc:  # noqa: BLE001 - v1's own bare try/except (`Birthdays.py:47-48`)
        log.warning("birthday.pin_failed", group_id=group_id, error=str(exc))
    await bot.send_message(group_id, "🎂")

    await _schedule_followup(ctx, group_id=group_id, lang=lang)
    log.info("birthday.collage_sent", group_id=group_id, targets=len(targets))


# --------------------------------------------------------------- the follow-up


async def next_birthdays_followup(ctx: dict[str, Any], *, group_id: int, lang: str) -> None:
    """The durable replacement for v1's `threading.Timer(900, next_birthdays,
    ...)` follow-up (D-BD-2) — same target text `cb_gateway.handlers.nextbirthday`
    builds for the manual command, `cb_core.birthdays` shares between them so
    the two call sites cannot drift.
    """
    parent = context_from_carrier(ctx.get("trace_carrier"))
    start = time.perf_counter()
    outcome = "ok"
    token = None
    try:
        from opentelemetry import context as otel_context

        token = otel_context.attach(parent)
        with span("job.next_birthdays_followup", kind=SpanKind.CONSUMER):
            today = datetime.now(UTC).date()
            text = await birthdays.next_birthdays_text(lang, today)
            await ctx["bot"].send_message(group_id, text)
    except Exception:
        outcome = "error"
        log.exception("job.failed", job="next_birthdays_followup")
        raise
    finally:
        if token is not None:
            from opentelemetry import context as otel_context

            otel_context.detach(token)
        job_duration.labels(job="next_birthdays_followup", outcome=outcome).observe(
            time.perf_counter() - start
        )


__all__ = [
    "birthday_photo_total",
    "next_birthdays_followup",
    "post_birthday_collage",
]
