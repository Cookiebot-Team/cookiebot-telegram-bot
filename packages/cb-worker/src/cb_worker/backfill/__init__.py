"""Cutover backfills: the parts of v1's data that a pure ETL mapper cannot move.

`cb_worker.importer` is document-in, row-out and deliberately I/O-free at the
mapping layer, which is what makes seven of v1's eight collections testable
without a network. One collection does not fit that shape at all:

    randomdatabase  _id=chat_id(str)  idMessage, idMedia

Every row is a *pointer* to a still-live Telegram message
(`RandomDatabase.java`), never bytes and never a hash — v1's `/random` forwards
the original message rather than re-sending stored content
(`SocialContent.py:198-206`). v2 stores the bytes, and `media_objects` requires
`content_hash`, `blob_key` and `byte_size` `NOT NULL` because the media layer
dedupes by content hash, so `mappers.map_randomdatabase` skips every document
and says why. Inventing a hash there would corrupt dedupe for every later write
to the same group.

Moving it needs what a mapper may not do: ask Telegram for the file behind each
`idMedia`, download it, hash the real bytes, and write a genuine
`media_objects` row through `cb_core.storage.media()`. That is this package.

It is a **command, not a job**, for the same reason the importer is: a cutover
step runs manually, repeatedly, while v1 is still serving — and again at
cutover to catch the delta — rather than on a schedule
(`.specs/features/platform_migration_etl/spec.md`, "No worker job triggers an
import automatically. This is deliberate").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Outcome = Literal["imported", "skipped", "failed"]


@dataclass(frozen=True, slots=True)
class PointerResult:
    """What happened to one `randomdatabase` document."""

    group_id: int | None
    file_id: str
    outcome: Outcome
    detail: str


@dataclass(slots=True)
class BackfillReport:
    """Counts plus every non-`imported` row, so a run can be read at a glance
    and audited in detail — the same split `importer.ImportReport` makes."""

    read: int = 0
    imported: int = 0
    skipped: int = 0
    failed: int = 0
    results: list[PointerResult] = field(default_factory=list)

    def record(self, result: PointerResult) -> None:
        self.read += 1
        if result.outcome == "imported":
            self.imported += 1
        elif result.outcome == "skipped":
            self.skipped += 1
        else:
            self.failed += 1
        if result.outcome != "imported":
            self.results.append(result)


__all__ = ["BackfillReport", "Outcome", "PointerResult"]
