"""`randomdatabase` -> `media_objects`: download what each v1 pointer points at.

The one collection `cb_worker.importer` cannot move (see this package's
`__init__`). One document is `{_id: chat_id, idMessage, idMedia}`; one row of
`media_objects` needs real bytes. So: ask Telegram for the file behind
`idMedia`, download it, and hand it to `cb_core.storage.media().put`, which
hashes it, writes the blob once and registers the per-group reference — the
same call `fun_random`'s live pooling handler makes for a new photo
(`cb_gateway/handlers/fun_random.py:_pool`), so a backfilled row and a
naturally-pooled one are indistinguishable afterwards.

## What can go wrong, and what each failure means

A v1 pointer is years old, and Telegram is under no obligation to still serve
it. Every one of these is *expected*, counted, and never fatal to the run:

* **The file id has expired or was never resolvable by this bot.** `getFile`
  answers 400. A `file_id` is bot-scoped: only the bot that saw the message can
  resolve it, which is why `--skin` selects a bot from `CB_BOT_TOKENS` and why
  running this with the wrong brand's token fails every row rather than some.
* **The message was deleted.** Same 400; indistinguishable from the above and
  reported the same way.
* **The file is larger than the Bot API will return.** 20 MB against
  `api.telegram.org`, 2 GB against a self-hosted server — one more reason
  `docs/deploy.mdx` runs a local Bot API server.
* **The group is not in `groups` yet.** `media_objects.group_id` is a foreign
  key, so the ordinary import has to have run first. Skipped with that reason
  rather than failing the run, because it is a sequencing mistake with an
  obvious fix.

## Idempotence, and why it is cheap the second time

`media().put` is already idempotent by content hash, so re-running writes no
duplicates — but it would re-download every file to find that out. Instead each
pointer is checked against `media_objects.telegram_file_id` for that group
first: one single-shard query (`group_id` is the distribution column, and it is
in the `WHERE`), and a hit skips the download entirely. That makes a resumed
run cost one query per already-imported pointer instead of one download.

The check is deliberately on `telegram_file_id` rather than content hash: the
hash is only knowable *after* the download this is trying to avoid.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from cb_core import db, storage
from cb_core.logging import get_logger
from cb_worker.backfill import BackfillReport, PointerResult
from cb_worker.importer.source import MongoSource

log = get_logger("cb.backfill.random_media")

COLLECTION = "randomdatabase"

#: `getFile`'s `file_path` extension -> the `media_objects.kind` to store under.
#: v1 pooled photos and videos only (`COOKIEBOT.py:165-172` calls
#: `add_to_random_database` from the photo and video branches), and Telegram's
#: own paths are `photos/file_N.jpg` / `videos/file_N.mp4`, so this table is
#: short by construction. Anything else is stored as a photo rather than
#: skipped — the bytes are real either way, and a wrong `kind` on a handful of
#: rows is a smaller loss than dropping them.
_KIND_BY_SUFFIX: dict[str, str] = {
    ".jpg": "photo",
    ".jpeg": "photo",
    ".png": "photo",
    ".webp": "photo",
    ".mp4": "video",
    ".mov": "video",
    ".gif": "animation",
}

_DEFAULT_KIND = "photo"

_EXISTS = """
SELECT 1
  FROM media_objects
 WHERE group_id = $1
   AND telegram_file_id = $2
 LIMIT 1
"""

_GROUP_EXISTS = "SELECT 1 FROM groups WHERE group_id = $1"


def kind_for_path(file_path: str) -> str:
    """`photos/file_12.jpg` -> `"photo"`. See `_KIND_BY_SUFFIX` for why the
    fallback is a kind rather than a skip."""
    lowered = file_path.lower()
    for suffix, kind in _KIND_BY_SUFFIX.items():
        if lowered.endswith(suffix):
            return kind
    return _DEFAULT_KIND


def parse_pointer(doc: Mapping[str, Any]) -> tuple[int, str] | None:
    """`{_id: "-1001234", idMedia: "AgAC..."}` -> `(group_id, file_id)`.

    `None` when the document cannot be used at all: v1 writes every id as a
    string (`RandomDatabase.java`), and one that will not parse as an integer
    is skipped and counted rather than guessed at — the same rule
    `importer.mappers` applies to every other collection.
    """
    raw_id = doc.get("_id")
    file_id = doc.get("idMedia")
    if not isinstance(file_id, str) or not file_id:
        return None
    try:
        group_id = int(str(raw_id))
    except (TypeError, ValueError):
        return None
    return group_id, file_id


async def _already_imported(group_id: int, file_id: str) -> bool:
    row = await db.fetchrow(_EXISTS, group_id, file_id, name="backfill_random_exists")
    return row is not None


async def _group_known(group_id: int) -> bool:
    row = await db.fetchrow(_GROUP_EXISTS, group_id, name="backfill_random_group")
    return row is not None


async def backfill_pointer(
    bot: Bot, doc: Mapping[str, Any], *, dry_run: bool = False
) -> PointerResult:
    """One document, end to end. Never raises: every outcome is a result."""
    parsed = parse_pointer(doc)
    if parsed is None:
        return PointerResult(None, str(doc.get("idMedia", "")), "skipped", "unusable pointer")
    group_id, file_id = parsed

    if not await _group_known(group_id):
        return PointerResult(
            group_id, file_id, "skipped", "group not imported yet - run import-mongo first"
        )
    if await _already_imported(group_id, file_id):
        return PointerResult(group_id, file_id, "skipped", "already imported")
    if dry_run:
        return PointerResult(group_id, file_id, "skipped", "dry run")

    try:
        file = await bot.get_file(file_id)
        buffer = await bot.download_file(file.file_path or "")
    except TelegramAPIError as exc:
        # Expected for an old pointer: deleted message, expired file id, or a
        # file this bot never saw (module docstring).
        return PointerResult(group_id, file_id, "failed", f"telegram: {exc}")
    except Exception as exc:  # noqa: BLE001 - a transport failure is one row, not the run
        return PointerResult(group_id, file_id, "failed", f"download: {exc}")

    if buffer is None:
        return PointerResult(group_id, file_id, "failed", "download returned nothing")
    data = buffer.read()
    if not data:
        return PointerResult(group_id, file_id, "failed", "downloaded zero bytes")

    kind = kind_for_path(file.file_path or "")
    ref = await storage.media().put(
        group_id,
        kind,
        data,
        telegram_file_id=file_id,
        # v1 never recorded who uploaded a pooled item — the pointer has no
        # user id at all — so this stays NULL rather than being invented.
        uploaded_by=None,
        # v1 refused to pool anything from a group whose title looked NSFW
        # (`SocialContent.py:191-196`), so every surviving pointer already
        # passed that filter; `fun_random`'s live pooling writes `sfw=True`
        # for exactly the same reason.
        sfw=True,
    )
    detail = "deduplicated" if ref.deduplicated else f"{ref.byte_size} bytes"
    return PointerResult(group_id, file_id, "imported", f"{kind}: {detail}")


async def run_backfill(
    source: MongoSource,
    bot: Bot,
    *,
    dry_run: bool = False,
    limit: int | None = None,
    on_progress: Callable[[BackfillReport], None] | None = None,
) -> BackfillReport:
    """Every `randomdatabase` document, in source order.

    Sequential on purpose: this is a download per row against Telegram's rate
    limits, and the run is a background cutover step nobody is waiting on. A
    concurrent version would need a semaphore tuned to a limit Telegram does
    not publish.

    `on_progress` fires after each pointer with the running report — the same
    "progress hook, not a `rich` dependency" split `importer.run_import` makes,
    so this module stays importable and testable without a rendering library.
    """
    report = BackfillReport()
    if COLLECTION not in source.collections():
        log.warning("backfill.collection.unavailable", collection=COLLECTION)
        return report

    for doc in source.read(COLLECTION):
        result = await backfill_pointer(bot, doc, dry_run=dry_run)
        report.record(result)
        if on_progress is not None:
            on_progress(report)
        if result.outcome == "failed":
            log.info(
                "backfill.pointer.failed",
                group_id=result.group_id,
                detail=result.detail,
            )
        if limit is not None and report.read >= limit:
            break

    log.info(
        "backfill.done",
        dry_run=dry_run,
        read=report.read,
        imported=report.imported,
        skipped=report.skipped,
        failed=report.failed,
    )
    return report


__all__ = [
    "COLLECTION",
    "backfill_pointer",
    "kind_for_path",
    "parse_pointer",
    "run_backfill",
]
