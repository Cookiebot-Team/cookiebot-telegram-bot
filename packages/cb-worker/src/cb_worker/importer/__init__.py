"""Mongo -> Citus import: the contract the three layers build against.

v1's data lives in the Java backend's MongoDB (`../COOKIEBOT-backend`, one
`@Document` class per collection). Cutting over means moving it into the v2
schema without a maintenance window, which is why this is an importer rather
than a one-shot script: every write is an idempotent upsert keyed on the natural
key, so it can run repeatedly while v1 is still serving, and again at cutover to
catch the delta.

Three layers, deliberately separate:

* `source.py`   — where documents come from. A live server (`mongodb://…`) or a
                  `mongodump` BSON directory. Nothing downstream knows which.
* `mappers.py`  — one pure function per collection, document -> row. No I/O, so
                  the shape rules (string ids, `"9999"` sentinels, `stickerSpamLimit`
                  arriving as a String) are unit-testable without any database.
* `loader.py`   — batched upserts into Citus, in foreign-key order.

The shapes are transcribed from the Java entities, which AGENTS.md names as the
source of truth for stored data:

    configs         _id=chat_id(str)  furbots, stickerSpamLimit(str!), timeWithoutSendingImages,
                    timeCaptcha, functionsFun, functionsUtility, sfw, language,
                    publisherPost, publisherAsk, publisherMembersOnly, threadPosts(str), maxPosts
    rules           _id=chat_id(str)  rules
    welcomes        _id=chat_id(str)  message
    users           _id=user_id(str)  username, firstName, lastName, languageCode, birthdate
    blacklist       _id=subject_id(str)
    groups          groupId, name, imageUrl, adminUsers[]
    randomdatabase  _id=chat_id(str)  idMessage, idMedia
    stickerdatabase _id=file_id(str)  -- the Telegram sticker file_id itself, not a chat/user id

Every id is a **string** in Mongo and a `bigint` here, with one exception:
`stickerdatabase`'s `_id` is not a Telegram chat/user id at all, it *is* the
sticker `file_id` (`mappers.map_stickerdatabase`'s own docstring), so it is
kept as text rather than parsed. Every other collection's id that will not
parse as an integer is skipped and counted, never guessed at.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

#: One Mongo document, already BSON-decoded.
Document = Mapping[str, Any]


class MongoSource(Protocol):
    """Where documents come from. Implemented for a live server and for a dump."""

    def collections(self) -> Sequence[str]:
        """Collection names this source can serve."""
        ...

    def read(self, collection: str) -> Iterator[Document]:
        """Stream every document. Must not load the whole collection into memory —
        v1's own backend did exactly that and it is why the random pool was slow."""
        ...

    def count(self, collection: str) -> int | None:
        """Document count when the source knows it cheaply, else None (progress only)."""
        ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class Skipped:
    """A document that could not be mapped, kept for the report rather than dropped."""

    collection: str
    document_id: str
    reason: str


@dataclass(slots=True)
class MappedRows:
    """What one collection's mapper produced.

    `rows` is keyed by target table because a single Mongo document can populate
    more than one (a `groups` document carries both the group and its admins).
    """

    rows: dict[str, list[tuple[Any, ...]]] = field(default_factory=dict)
    skipped: list[Skipped] = field(default_factory=list)

    def add(self, table: str, row: tuple[Any, ...]) -> None:
        self.rows.setdefault(table, []).append(row)

    def skip(self, collection: str, document_id: str, reason: str) -> None:
        self.skipped.append(Skipped(collection, document_id, reason))


@dataclass(frozen=True, slots=True)
class TableLoad:
    """How to write one target table.

    `conflict_columns` is the natural key the upsert keys on — always including
    `group_id` for a distributed table, because Citus requires the distribution
    column in any unique constraint (AGENTS.md §4.3).
    """

    table: str
    columns: tuple[str, ...]
    conflict_columns: tuple[str, ...]
    update_columns: tuple[str, ...]


@dataclass(slots=True)
class ImportReport:
    """Per-collection outcome. Printed at the end and asserted in tests."""

    read: dict[str, int] = field(default_factory=dict)
    written: dict[str, int] = field(default_factory=dict)
    skipped: list[Skipped] = field(default_factory=list)

    def total_written(self) -> int:
        return sum(self.written.values())

    def as_lines(self) -> list[str]:
        lines = [f"{'collection/table':<20} {'read':>8} {'written':>8}"]
        for name in sorted(set(self.read) | set(self.written)):
            lines.append(f"{name:<20} {self.read.get(name, 0):>8} {self.written.get(name, 0):>8}")
        if self.skipped:
            lines.append(f"\nskipped {len(self.skipped)}:")
            reasons: dict[str, int] = {}
            for item in self.skipped:
                reasons[item.reason] = reasons.get(item.reason, 0) + 1
            lines.extend(f"  {count:>6}  {reason}" for reason, count in sorted(reasons.items()))
        return lines


__all__ = [
    "Document",
    "ImportReport",
    "MappedRows",
    "MongoSource",
    "Skipped",
    "TableLoad",
]
