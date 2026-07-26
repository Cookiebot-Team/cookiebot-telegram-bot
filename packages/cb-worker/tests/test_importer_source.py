"""Unit tests for the Mongo source layer — dump discovery and streaming.

No live MongoDB required: `DumpMongoSource` is exercised against real BSON
files built with `bson.encode` in `tmp_path`, which is also what a real
`mongodump` produces. The one test that needs a live server is marked
`integration` and skips cleanly when `CB_TEST_MONGO_URI` is unset.
"""

from __future__ import annotations

import gzip
import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import bson
import pytest

from cb_core.settings import Settings
from cb_worker.importer.source import (
    DumpMongoSource,
    LiveMongoSource,
    MongoSourceError,
    open_source,
)


def _write_bson(path: Path, docs: Iterable[Mapping[str, Any]]) -> None:
    path.write_bytes(b"".join(bson.encode(doc) for doc in docs))


def _write_metadata(path: Path) -> None:
    path.write_text('{"indexes": []}')


class TestDumpLayouts:
    def test_flat_layout_streams_documents(self, tmp_path: Path) -> None:
        dump_dir = tmp_path / "dump"
        dump_dir.mkdir()
        _write_bson(
            dump_dir / "configs.bson",
            [{"_id": "1", "furbots": True}, {"_id": "2", "furbots": False}],
        )
        _write_bson(dump_dir / "welcomes.bson", [])

        source = DumpMongoSource(dump_dir)
        try:
            assert set(source.collections()) == {"configs", "welcomes"}
            configs = list(source.read("configs"))
            assert [doc["_id"] for doc in configs] == ["1", "2"]
            assert list(source.read("welcomes")) == []
            assert source.count("configs") is None
        finally:
            source.close()

    def test_nested_database_layout_streams_documents(self, tmp_path: Path) -> None:
        dump_dir = tmp_path / "dump"
        nested = dump_dir / "cookiebot"
        nested.mkdir(parents=True)
        _write_bson(nested / "users.bson", [{"_id": "42", "username": "alice"}])

        source = DumpMongoSource(dump_dir, database="cookiebot")
        try:
            assert source.collections() == ["users"]
            assert [doc["_id"] for doc in source.read("users")] == ["42"]
        finally:
            source.close()

    def test_nested_layout_auto_detected_without_matching_database_name(
        self, tmp_path: Path
    ) -> None:
        # mongodump's own directory name need not match `mongo_database` — the
        # sole subdirectory is used when nothing else matches.
        dump_dir = tmp_path / "dump"
        nested = dump_dir / "some_other_db_name"
        nested.mkdir(parents=True)
        _write_bson(nested / "rules.bson", [{"_id": "7", "rules": "be nice"}])

        source = DumpMongoSource(dump_dir, database="cookiebot")
        try:
            assert source.collections() == ["rules"]
        finally:
            source.close()

    def test_metadata_json_is_ignored(self, tmp_path: Path) -> None:
        dump_dir = tmp_path / "dump"
        dump_dir.mkdir()
        _write_bson(dump_dir / "blacklist.bson", [{"_id": "9"}])
        _write_metadata(dump_dir / "blacklist.metadata.json")

        source = DumpMongoSource(dump_dir)
        try:
            assert source.collections() == ["blacklist"]
        finally:
            source.close()

    def test_missing_collection_raises_clear_error(self, tmp_path: Path) -> None:
        dump_dir = tmp_path / "dump"
        dump_dir.mkdir()
        _write_bson(dump_dir / "configs.bson", [{"_id": "1"}])

        source = DumpMongoSource(dump_dir)
        try:
            with pytest.raises(MongoSourceError, match="stickerdatabase"):
                source.read("stickerdatabase")
        finally:
            source.close()

    def test_missing_directory_raises_clear_error(self, tmp_path: Path) -> None:
        with pytest.raises(MongoSourceError, match="not found"):
            DumpMongoSource(tmp_path / "does-not-exist")

    def test_gzipped_dump_raises_clear_error(self, tmp_path: Path) -> None:
        dump_dir = tmp_path / "dump"
        dump_dir.mkdir()
        raw = b"".join(bson.encode(doc) for doc in [{"_id": "1"}])
        (dump_dir / "groups.bson.gz").write_bytes(gzip.compress(raw))

        source = DumpMongoSource(dump_dir)
        try:
            assert source.collections() == ["groups"]
            with pytest.raises(MongoSourceError, match="gzipped dumps are not supported"):
                source.read("groups")
        finally:
            source.close()

    def test_uncompressed_bson_wins_over_gzipped_sibling(self, tmp_path: Path) -> None:
        dump_dir = tmp_path / "dump"
        dump_dir.mkdir()
        _write_bson(dump_dir / "groups.bson", [{"_id": "1"}])
        raw = b"".join(bson.encode(doc) for doc in [{"_id": "1"}])
        (dump_dir / "groups.bson.gz").write_bytes(gzip.compress(raw))

        source = DumpMongoSource(dump_dir)
        try:
            assert [doc["_id"] for doc in source.read("groups")] == ["1"]
        finally:
            source.close()


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        pytest.param(
            {"mongo_uri": "", "mongo_dump_dir": ""},
            "Neither CB_MONGO_URI nor CB_MONGO_DUMP_DIR",
            id="unconfigured",
        ),
        pytest.param(
            {"mongo_uri": "mongodb://localhost/x", "mongo_dump_dir": "/tmp/dump"},
            "Both CB_MONGO_URI and CB_MONGO_DUMP_DIR",
            id="ambiguous",
        ),
    ],
)
def test_open_source_rejects_bad_configuration(kwargs: dict[str, str], match: str) -> None:
    settings = Settings(**kwargs)
    with pytest.raises(ValueError, match=match):
        open_source(settings)


def test_open_source_picks_dump_source(tmp_path: Path) -> None:
    dump_dir = tmp_path / "dump"
    dump_dir.mkdir()
    _write_bson(dump_dir / "configs.bson", [{"_id": "1"}])

    settings = Settings(mongo_uri="", mongo_dump_dir=str(dump_dir))
    source = open_source(settings)
    try:
        assert isinstance(source, DumpMongoSource)
        assert source.collections() == ["configs"]
    finally:
        source.close()


@pytest.mark.integration
def test_live_source_against_real_mongo() -> None:
    uri = os.environ.get("CB_TEST_MONGO_URI")
    if not uri:
        pytest.skip("CB_TEST_MONGO_URI not set — no live MongoDB to test against")
    source = LiveMongoSource(uri, "cookiebot")
    try:
        collections = source.collections()
        assert isinstance(collections, list)
        for name in collections:
            count = source.count(name)
            assert count is None or count >= 0
    finally:
        source.close()


def test_live_source_bad_uri_fails_fast_with_clear_message() -> None:
    # A bogus but well-formed URI must fail within the configured timeout
    # rather than hang, and the message must not echo the URI back.
    source = LiveMongoSource(
        "mongodb://nobody:secret@127.0.0.1:1/does-not-matter",
        "cookiebot",
        timeout_ms=200,
    )
    try:
        with pytest.raises(MongoSourceError) as exc_info:
            source.collections()
        message = str(exc_info.value)
        assert "127.0.0.1" in message
        assert "secret" not in message
        assert "nobody" not in message
    finally:
        source.close()
