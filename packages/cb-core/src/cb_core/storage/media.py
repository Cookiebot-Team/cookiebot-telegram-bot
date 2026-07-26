"""Media service: blob store + Postgres reference rows.

The split matters for Citus. The blob is global and content-addressed; the
*reference* row is per-group and lives on the group's shard, so:

* dedupe is a hash lookup, not a scan (v1 had none — `SocialContent.py:191-196`);
* "give me a random photo from this group" is a **single-shard router query**,
  not a cross-shard scan and not a full-collection load into application memory
  the way `RandomDatabaseService.getRandom()` did it;
* "this group left, drop its media" is one local DELETE.

Every query in here is filtered by `group_id`, which is the distribution column,
so Citus routes it to one node and no data crosses the network between shards.
"""

from __future__ import annotations

from uuid import UUID

import asyncpg
import msgspec

from cb_core import db, metrics
from cb_core.ids import uuid7
from cb_core.logging import get_logger
from cb_core.storage.base import BlobStore
from cb_core.storage.keys import VALID_KINDS, hash_and_key

log = get_logger("cb.media")


class MediaRef(msgspec.Struct, frozen=True):
    media_id: UUID
    group_id: int
    kind: str
    content_hash: str
    blob_key: str
    byte_size: int
    content_type: str | None = None
    telegram_file_id: str | None = None
    deduplicated: bool = False


_INSERT = """
INSERT INTO media_objects (
    media_id, group_id, kind, content_hash, blob_key, byte_size,
    content_type, telegram_file_id, uploaded_by, sfw
) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
-- EXCLUDED.last_seen_at, not now(): Citus refuses non-IMMUTABLE functions in the
-- DO UPDATE SET of an insert on a distributed table. EXCLUDED carries the column
-- default (now()), evaluated once for the statement, so the timestamp is the same.
ON CONFLICT (group_id, content_hash) DO UPDATE
    SET telegram_file_id = COALESCE(EXCLUDED.telegram_file_id, media_objects.telegram_file_id),
        last_seen_at = EXCLUDED.last_seen_at
RETURNING media_id, blob_key, byte_size, content_type, (xmax <> 0) AS existed
"""

_SELECT_BY_HASH = """
SELECT media_id, blob_key, byte_size, content_type, telegram_file_id
FROM media_objects
WHERE group_id = $1 AND content_hash = $2
"""

# Router query: group_id is the distribution column, so this touches one shard.
_SELECT_RANDOM = """
SELECT media_id, group_id, kind, content_hash, blob_key, byte_size,
       content_type, telegram_file_id
FROM media_objects
WHERE group_id = $1 AND kind = ANY($2::text[]) AND (NOT $3::boolean OR sfw)
ORDER BY random()
LIMIT 1
"""


class MediaService:
    def __init__(self, store: BlobStore) -> None:
        self._store = store

    @property
    def store(self) -> BlobStore:
        return self._store

    async def put(
        self,
        group_id: int,
        kind: str,
        data: bytes,
        *,
        uploaded_by: int | None = None,
        content_type: str | None = None,
        telegram_file_id: str | None = None,
        sfw: bool = True,
    ) -> MediaRef:
        """Store bytes and register them for this group.

        Idempotent by content: uploading the same bytes twice in the same group
        returns the existing reference and skips the blob write.
        """
        if kind not in VALID_KINDS:
            raise ValueError(f"unknown media kind {kind!r}")

        content_hash, key = hash_and_key(kind, data)

        existing = await db.fetchrow(_SELECT_BY_HASH, group_id, content_hash, name="media_by_hash")
        if existing is not None:
            metrics.media_dedupe_total.labels(kind=kind, result="hit").inc()
            return MediaRef(
                media_id=existing["media_id"],
                group_id=group_id,
                kind=kind,
                content_hash=content_hash,
                blob_key=existing["blob_key"],
                byte_size=existing["byte_size"],
                content_type=existing["content_type"],
                telegram_file_id=existing["telegram_file_id"],
                deduplicated=True,
            )

        # Another group may already hold this exact blob — check before paying
        # for the upload.
        if not await self._store.exists(key):
            await self._store.put(key, data, content_type=content_type)
            metrics.storage_bytes_total.labels(backend=self._store.scheme, kind=kind).inc(len(data))
            metrics.media_dedupe_total.labels(kind=kind, result="miss").inc()
        else:
            metrics.media_dedupe_total.labels(kind=kind, result="blob_shared").inc()

        # media_blobs is a reference table, so this write replicates; it only runs
        # on a genuinely new blob, never on the dedupe path.
        await db.execute(
            """
            INSERT INTO media_blobs (content_hash, blob_key, kind, byte_size, content_type, backend)
            VALUES ($1,$2,$3,$4,$5,$6) ON CONFLICT (content_hash) DO NOTHING
            """,
            content_hash,
            key,
            kind,
            len(data),
            content_type,
            self._store.scheme,
            name="media_blob_register",
        )

        row = await db.fetchrow(
            _INSERT,
            uuid7(),
            group_id,
            kind,
            content_hash,
            key,
            len(data),
            content_type,
            telegram_file_id,
            uploaded_by,
            sfw,
            name="media_insert",
        )
        assert row is not None
        return MediaRef(
            media_id=row["media_id"],
            group_id=group_id,
            kind=kind,
            content_hash=content_hash,
            blob_key=row["blob_key"],
            byte_size=row["byte_size"],
            content_type=row["content_type"],
            telegram_file_id=telegram_file_id,
            deduplicated=bool(row["existed"]),
        )

    async def get_bytes(self, ref: MediaRef) -> bytes:
        return await self._store.get(ref.blob_key)

    async def signed_url(self, ref: MediaRef, *, expires_in: int = 3600) -> str:
        return await self._store.signed_url(ref.blob_key, expires_in=expires_in)

    async def random(
        self,
        group_id: int,
        kinds: tuple[str, ...] = ("photo", "animation", "video"),
        *,
        sfw_only: bool = True,
    ) -> MediaRef | None:
        """One random item for `/random` (fun_random.feature).

        Selection happens in Postgres on a single shard. v1's equivalent loaded
        the entire `randomdatabase` collection into the JVM on every call
        (`RandomDatabaseService.getRandom`).
        """
        row = await db.fetchrow(
            _SELECT_RANDOM, group_id, list(kinds), sfw_only, name="media_random"
        )
        return _row_to_ref(row) if row is not None else None

    async def forget_group(self, group_id: int) -> int:
        """Drop this group's references. Blobs are collected separately once unreferenced."""
        result = await db.execute(
            "DELETE FROM media_objects WHERE group_id = $1", group_id, name="media_forget_group"
        )
        log.info("media.group_forgotten", group_id=group_id, result=result)
        return _rows_affected(result)

    async def unreferenced_blobs(self, limit: int = 1000) -> list[str]:
        """Blob keys no group references any more — the GC worklist.

        Written as an uncorrelated `NOT IN`, not `NOT EXISTS`: `media_blobs` is a
        reference table and `media_objects` is distributed, and Citus rejects a
        correlated subquery in that direction —

            correlated subqueries are not supported when the FROM clause
            contains a reference table

        Uncorrelated, Citus recursively plans it: each shard scans locally, the
        distinct hashes come back as one intermediate result, and the anti-join
        runs against that. Still a full scan, so it belongs in the scheduled
        worker, never on a reply path.

        `content_hash` is NOT NULL in both tables, so `NOT IN` cannot go
        three-valued on us.
        """
        rows = await db.fetch(
            """
            SELECT b.blob_key FROM media_blobs b
            WHERE b.content_hash NOT IN (SELECT DISTINCT content_hash FROM media_objects)
            ORDER BY b.created_at
            LIMIT $1
            """,
            limit,
            name="media_unreferenced",
        )
        return [r["blob_key"] for r in rows]

    async def collect_garbage(self, limit: int = 500) -> int:
        """Delete unreferenced blobs from the object store and the registry."""
        keys = await self.unreferenced_blobs(limit)
        for key in keys:
            await self._store.delete(key)
        if keys:
            await db.execute(
                "DELETE FROM media_blobs WHERE blob_key = ANY($1::text[])",
                keys,
                name="media_blob_gc",
            )
            log.info("media.gc", deleted=len(keys), backend=self._store.scheme)
        return len(keys)


def _row_to_ref(row: asyncpg.Record) -> MediaRef:
    return MediaRef(
        media_id=row["media_id"],
        group_id=row["group_id"],
        kind=row["kind"],
        content_hash=row["content_hash"],
        blob_key=row["blob_key"],
        byte_size=row["byte_size"],
        content_type=row["content_type"],
        telegram_file_id=row["telegram_file_id"],
    )


def _rows_affected(result: str) -> int:
    # asyncpg returns command tags like "DELETE 12"
    parts = result.rsplit(" ", 1)
    return int(parts[1]) if len(parts) == 2 and parts[1].isdigit() else 0
