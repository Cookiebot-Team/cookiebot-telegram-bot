"""Where documents come from: a live v1 MongoDB or a `mongodump` directory.

`open_source` is the only entry point downstream code should call — it reads
`Settings` and picks one of `LiveMongoSource` / `DumpMongoSource` per the
contract in `cb_worker/importer/__init__.py` ("exactly one of these is used").

Both implementations stream: `read()` yields one document at a time (via
`Cursor` iteration for the live source, `bson.decode_file_iter` for a dump),
never `list(cursor)` and never a whole-file read — a v1 collection is exactly
the kind of thing v1's own backend loaded wholesale, which is why the random
pool was slow (see the module docstring one level up).
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path
from urllib.parse import urlsplit

import bson
import structlog
from pymongo import MongoClient
from pymongo.errors import PyMongoError

from cb_core.settings import Settings
from cb_worker.importer import Document, MongoSource

logger = structlog.get_logger(__name__)

#: A wrong URI or an unreachable host must fail in seconds, not hang forever
#: waiting for a replica set that does not exist.
_DEFAULT_TIMEOUT_MS = 5_000

_BSON_SUFFIX = ".bson"
_BSON_GZ_SUFFIX = ".bson.gz"


class MongoSourceError(RuntimeError):
    """A source-layer failure with a message that is safe to log and show —
    never carries a URI or credentials, only host/database/collection names."""


def _host_for_logging(uri: str) -> str:
    """Host (and port, if given) for logging only — never the full URI, which
    carries credentials in `mongodb://user:pass@host/...` form."""
    parsed = urlsplit(uri)
    return parsed.hostname or "<unknown-host>"


class LiveMongoSource:
    """Reads a running v1 MongoDB.

    Read-only by construction: every method issues a `find`, a `listCollections`
    or a count — never an insert/update/delete, never an index build, and no
    admin command beyond what counting needs.
    """

    def __init__(self, uri: str, database: str, *, timeout_ms: int = _DEFAULT_TIMEOUT_MS) -> None:
        self._host = _host_for_logging(uri)
        self._database_name = database
        try:
            self._client: MongoClient[Document] = MongoClient(
                uri,
                serverSelectionTimeoutMS=timeout_ms,
                connectTimeoutMS=timeout_ms,
            )
        except PyMongoError as exc:
            raise self._connection_error(exc) from exc
        self._db = self._client[database]
        logger.info("importer.mongo_source.live", host=self._host, database=database)

    def collections(self) -> Sequence[str]:
        try:
            return sorted(self._db.list_collection_names())
        except PyMongoError as exc:
            raise self._connection_error(exc) from exc

    def read(self, collection: str) -> Iterator[Document]:
        try:
            cursor = self._db[collection].find({})
            yield from cursor
        except PyMongoError as exc:
            raise self._connection_error(exc) from exc

    def count(self, collection: str) -> int | None:
        try:
            return self._db[collection].estimated_document_count()
        except PyMongoError as exc:
            raise self._connection_error(exc) from exc

    def close(self) -> None:
        self._client.close()

    def _connection_error(self, exc: PyMongoError) -> MongoSourceError:
        return MongoSourceError(
            f"Could not reach MongoDB at host={self._host!r} database={self._database_name!r}: {exc}"
        )


def _collection_name(path: Path) -> str:
    """Strip `.bson` / `.bson.gz`, never just the last suffix — `path.stem` on
    `configs.bson.gz` would wrongly yield `configs.bson`."""
    name = path.name
    if name.endswith(_BSON_GZ_SUFFIX):
        return name[: -len(_BSON_GZ_SUFFIX)]
    if name.endswith(_BSON_SUFFIX):
        return name[: -len(_BSON_SUFFIX)]
    return path.stem


def _resolve_root(directory: Path, database: str | None) -> Path:
    """A `mongodump` directory is either flat (`dump/<coll>.bson`) or nested
    under the database name (`dump/<db>/<coll>.bson`). Prefer the configured
    database name if that subdirectory exists; fall back to the directory
    itself if it already holds `.bson` files; fall back again to the sole
    subdirectory when there is exactly one (mongodump's default layout, whose
    directory name need not match `mongo_database`)."""
    if database:
        candidate = directory / database
        if candidate.is_dir():
            return candidate
    if any(directory.glob(f"*{_BSON_SUFFIX}")) or any(directory.glob(f"*{_BSON_GZ_SUFFIX}")):
        return directory
    subdirs = [p for p in directory.iterdir() if p.is_dir()]
    if len(subdirs) == 1:
        return subdirs[0]
    return directory


class DumpMongoSource:
    """Reads a `mongodump` BSON directory, streaming each collection file.

    `.metadata.json` sidecars are ignored — only `<collection>.bson` files are
    collections. `count()` always returns `None`: knowing it cheaply would mean
    pre-scanning the file, which defeats the point of streaming it.
    """

    def __init__(self, directory: Path, database: str | None = None) -> None:
        if not directory.is_dir():
            raise MongoSourceError(f"mongodump directory not found: {directory}")
        self._root = _resolve_root(directory, database)
        self._files: dict[str, Path] = {}
        for path in sorted(self._root.glob(f"*{_BSON_SUFFIX}")):
            self._files[_collection_name(path)] = path
        for path in sorted(self._root.glob(f"*{_BSON_GZ_SUFFIX}")):
            # A `.bson` file for the same collection, if present, wins — it is
            # the one we can actually stream.
            self._files.setdefault(_collection_name(path), path)
        logger.info(
            "importer.mongo_source.dump",
            root=str(self._root),
            collections=len(self._files),
        )

    def collections(self) -> Sequence[str]:
        return sorted(self._files)

    def read(self, collection: str) -> Iterator[Document]:
        path = self._files.get(collection)
        if path is None:
            raise MongoSourceError(
                f"collection {collection!r} not found in dump at {self._root}"
                f" (have: {', '.join(sorted(self._files)) or '<none>'})"
            )
        if path.name.endswith(_BSON_GZ_SUFFIX):
            raise MongoSourceError("gzipped dumps are not supported, run mongodump without --gzip")
        return self._stream(path)

    def count(self, collection: str) -> int | None:
        return None

    def close(self) -> None:
        pass

    def _stream(self, path: Path) -> Iterator[Document]:
        with path.open("rb") as fh:
            yield from bson.decode_file_iter(fh)


def open_source(settings: Settings) -> MongoSource:
    """Pick the configured source. Exactly one of `mongo_uri` / `mongo_dump_dir`
    must be set — both empty means no import is configured, both set is
    ambiguous, and neither error should make the caller guess which knob to
    check."""
    uri = settings.mongo_uri.strip()
    dump_dir = settings.mongo_dump_dir.strip()
    if uri and dump_dir:
        raise ValueError(
            "Both CB_MONGO_URI and CB_MONGO_DUMP_DIR are set; the importer needs "
            "exactly one Mongo source — unset whichever one is not intended."
        )
    if uri:
        return LiveMongoSource(uri, settings.mongo_database)
    if dump_dir:
        return DumpMongoSource(Path(dump_dir), settings.mongo_database)
    raise ValueError(
        "Neither CB_MONGO_URI nor CB_MONGO_DUMP_DIR is set; the importer needs "
        "one of them to know where v1's data is."
    )


__all__ = ["DumpMongoSource", "LiveMongoSource", "MongoSourceError", "open_source"]
