"""fun_meme — profile pictures pasted into a meme template.

v1: `meme`, `../COOKIEBOT-Telegram-Group-Bot/Bot/SocialContent.py:224-277`,
dispatched `COOKIEBOT.py:222-223` under the `funfunctions` gate. Contract:
`docs/contracts/fun_meme.md`.

The gate and the "more than five tags" refusal stay on the reply path
(`cb_gateway/handlers/meme.py`); everything here is a template fetch, N profile
photo downloads and a compositing pass — AGENTS.md §2.4, and `scripts/spec.py`
already said so ("image compositing is a worker job, not a reply-path call").

Four of v1's mechanisms are replaced, each for a reason recorded in the
contract:

* **The template pool** is `cb_core.storage`, not a 110 MB directory next to
  the code — see `cb_core/meme_templates.py`.
* **Profile pictures** come from the Bot API's `get_user_profile_photos`, not
  a `telegram.me` HTML scrape (v1's `get_profile_image`, `:279-292`) — the
  same replacement `fun_battle` and `x_giveaways` already made.
* **Compositing** is Pillow, not OpenCV. `util_birthday`'s collage already
  established Pillow here, and nothing in this job needs OpenCV's contour
  finding: the rectangles were computed offline into the CSV.
* **The output** never touches disk. v1 wrote `meme.png` into the process's
  working directory on every request (`:275`) — FEATURE-MAP D4, the same fixed
  filename shared by every concurrent call.

Does not import `cb_worker.main`; the telemetry wrapper is copied from
`youtube.py` for that reason.
"""

from __future__ import annotations

import asyncio
import io
import time
from typing import Any

from aiogram import Bot
from aiogram.types import BufferedInputFile
from opentelemetry.trace import SpanKind
from PIL import Image
from prometheus_client import Counter

from cb_core import members, storage
from cb_core.locales import get as locale_get
from cb_core.logging import get_logger
from cb_core.meme_templates import MemeTemplate, choose
from cb_core.metrics import job_duration
from cb_core.telemetry import context_from_carrier, span

log = get_logger("cb.worker.meme")

# outcome in sent|no_template|no_picture|error. Never a group id or a username.
meme_total = Counter("cb_worker_meme_total", "Memes composed by /meme", ["outcome"])


def paste_faces(
    template: Image.Image, rects: tuple[tuple[int, int, int, int], ...], faces: list[Image.Image]
) -> Image.Image:
    """v1's inner loop (`:256-273`), as one function.

    Each face is resized to its rectangle and pasted over it. v1 used
    `cv2.INTER_NEAREST`; `Image.Resampling.NEAREST` is the same filter, kept
    rather than upgraded because the deliberate crunchiness is the look.
    """
    canvas = template.convert("RGB")
    for x, y, w, h in rects[: len(faces)]:
        face = faces.pop(0).convert("RGB").resize((w, h), Image.Resampling.NEAREST)
        canvas.paste(face, (x, y))
    return canvas


def caption_for(names: list[str]) -> str:
    """v1 appends `f"@{chosen_member} "` per filled rectangle (`:274`), so the
    caption carries a trailing space. Preserved."""
    return "".join(f"@{name} " for name in names)


async def compose_meme(
    ctx: dict[str, Any], *, group_id: int, message_id: int, tagged: list[str], lang: str
) -> None:
    parent = context_from_carrier(ctx.get("trace_carrier"))
    start = time.perf_counter()
    outcome = "ok"
    token = None
    try:
        from opentelemetry import context as otel_context

        token = otel_context.attach(parent)
        with span("job.compose_meme", kind=SpanKind.CONSUMER):
            await _run(ctx["bot"], group_id, message_id, tagged, lang)
    except Exception:
        outcome = "error"
        log.exception("job.failed", job="compose_meme")
        raise
    finally:
        if token is not None:
            from opentelemetry import context as otel_context

            otel_context.detach(token)
        job_duration.labels(job="compose_meme", outcome=outcome).observe(
            time.perf_counter() - start
        )


async def _run(bot: Bot, group_id: int, message_id: int, tagged: list[str], lang: str) -> None:
    template = choose(len(tagged), lang)
    if template is None:
        # v1 has no branch here: `contours_green` is assigned only inside
        # `if suitable_templates:` and read unconditionally afterwards
        # (`:244-248`), so an empty pool is a NameError and the group hears
        # nothing. `meme_error` is v1's own "I couldn't build this" string.
        log.warning("meme.no_template", lang=lang, tagged=len(tagged))
        await _reply(bot, group_id, message_id, locale_get("meme_error", lang))
        meme_total.labels(outcome="no_template").inc()
        return

    image = await _load_template(template)
    if image is None:
        await _reply(bot, group_id, message_id, locale_get("meme_error", lang))
        meme_total.labels(outcome="error").inc()
        return

    faces, names = await _gather_faces(bot, group_id, tagged, len(template.blob_rects))
    if not faces:
        # v1's own dead end (`:269-272`): nobody usable had a picture.
        await _reply(bot, group_id, message_id, locale_get("meme_error", lang))
        meme_total.labels(outcome="no_picture").inc()
        return

    composed = await asyncio.to_thread(paste_faces, image, template.blob_rects, list(faces))
    buffer = io.BytesIO()
    composed.save(buffer, format="PNG")

    await bot.send_photo(
        group_id,
        BufferedInputFile(buffer.getvalue(), filename="meme.png"),
        caption=caption_for(names),
        reply_to_message_id=message_id,
    )
    meme_total.labels(outcome="sent").inc()


async def _load_template(template: MemeTemplate) -> Image.Image | None:
    try:
        data = await storage.store().get(template.storage_key)
    except Exception as exc:  # noqa: BLE001 - an unseeded store is a deployment gap
        log.warning("meme.template_missing", key=template.storage_key, error=str(exc))
        return None
    try:
        with Image.open(io.BytesIO(data)) as opened:
            image = opened.convert("RGB")
            image.load()
    except Exception as exc:  # noqa: BLE001 - a corrupt template is not a worker failure
        log.warning("meme.template_unreadable", key=template.storage_key, error=str(exc))
        return None
    return image


async def _gather_faces(
    bot: Bot, group_id: int, tagged: list[str], wanted: int
) -> tuple[list[Image.Image], list[str]]:
    """Tagged members first, then the rest of the roster — v1's two loops
    (`:257-268`), with the scrape replaced.

    v1's second loop is effectively dead: it picks `member['user']`, a *dict*,
    and hands it to `get_profile_image(username)`, which interpolates it into a
    URL. That never resolves, so v1 only ever fills rectangles from explicit
    tags. Here both halves work, because a roster entry carries a real
    `user_id` — the drift is recorded as D-ME-3.
    """
    roster = await members.roster(group_id)
    by_username = {m.username.lower(): m for m in roster if m.username}

    ordered: list[tuple[int, str]] = []
    seen: set[int] = set()
    for raw in tagged:
        member = by_username.get(raw.strip().split()[0].lower() if raw.strip() else "")
        if member is not None and member.user_id not in seen:
            seen.add(member.user_id)
            ordered.append((member.user_id, raw))
    # Not truncated to `wanted`: v1 keeps drawing from the roster until it
    # finds someone whose picture it can actually get (`:262-268`), so the
    # candidate list has to be longer than the number of rectangles.
    for member in roster:
        if member.user_id not in seen and member.username:
            seen.add(member.user_id)
            ordered.append((member.user_id, member.username))

    faces: list[Image.Image] = []
    names: list[str] = []
    for user_id, display in ordered:
        if len(faces) >= wanted:
            break
        face = await _profile_photo(bot, user_id)
        if face is None:
            continue  # v1 keeps looking too (`:259-268`)
        faces.append(face)
        names.append(display)
    return faces, names


async def _profile_photo(bot: Bot, user_id: int) -> Image.Image | None:
    try:
        photos = await bot.get_user_profile_photos(user_id, limit=1)
        if not photos.photos:
            return None
        buffer = await bot.download(photos.photos[0][-1].file_id)
    except Exception as exc:  # noqa: BLE001 - a private avatar is v1's own dead end
        log.info("meme.photo_unavailable", error=str(exc))
        return None
    if buffer is None:
        return None
    try:
        with Image.open(io.BytesIO(buffer.read())) as opened:
            image = opened.convert("RGB")
            image.load()
    except Exception as exc:  # noqa: BLE001 - same
        log.info("meme.photo_unreadable", error=str(exc))
        return None
    return image


async def _reply(bot: Bot, group_id: int, message_id: int, text: str) -> None:
    try:
        await bot.send_message(group_id, text, reply_to_message_id=message_id)
    except Exception as exc:  # noqa: BLE001 - the job's work is done either way
        log.warning("meme.reply_failed", error=str(exc))


__all__ = ["caption_for", "compose_meme", "meme_total", "paste_faces"]
