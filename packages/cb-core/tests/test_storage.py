"""Unit tests for storage — key derivation and the memory backend.

No infrastructure: the memory backend is the same code path as S3/GCS through
obstore, so the contract is exercised without a cloud or a container.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cb_core.storage import ObjectNotFoundError, StorageError, store_from_uri
from cb_core.storage.keys import blob_key, derived_key, extension_for, hash_and_key
from cb_core.storage.obstore_backend import ObstoreBlobStore


class TestKeys:
    def test_key_is_content_addressed(self) -> None:
        h1, k1 = hash_and_key("photo", b"same bytes")
        h2, k2 = hash_and_key("photo", b"same bytes")
        assert (h1, k1) == (h2, k2)

    def test_different_bytes_different_key(self) -> None:
        _, k1 = hash_and_key("photo", b"a")
        _, k2 = hash_and_key("photo", b"b")
        assert k1 != k2

    def test_kind_selects_extension(self) -> None:
        assert blob_key("photo", "abcdef").endswith(".jpg")
        assert blob_key("sticker", "abcdef").endswith(".webp")
        assert blob_key("voice", "abcdef").endswith(".ogg")

    def test_fan_out_directory(self) -> None:
        # Two hex chars keep listings usable on lexicographically paginated stores.
        assert blob_key("photo", "ab1234") == "media/photo/ab/ab1234.jpg"

    def test_unknown_kind_rejected(self) -> None:
        with pytest.raises(ValueError):
            blob_key("hologram", "abcdef")

    def test_short_hash_rejected(self) -> None:
        with pytest.raises(ValueError):
            blob_key("photo", "ab")

    def test_derived_keys_are_deterministic(self) -> None:
        a = derived_key("abcdef123456", "thumbnail")
        b = derived_key("abcdef123456", "thumbnail")
        assert a == b == "derived/thumbnail/ab/abcdef123456.png"

    def test_derived_variant_must_be_identifier(self) -> None:
        with pytest.raises(ValueError):
            derived_key("abcdef", "../../etc/passwd")

    def test_unknown_kind_extension_falls_back(self) -> None:
        assert extension_for("mystery") == ".bin"


class TestMemoryBackend:
    @pytest.fixture
    def store(self) -> ObstoreBlobStore:
        return store_from_uri("memory://")

    async def test_put_get_roundtrip(self, store: ObstoreBlobStore) -> None:
        meta = await store.put("a/b.bin", b"hello", content_type="text/plain")
        assert meta.size == 5
        assert await store.get("a/b.bin") == b"hello"

    async def test_head_reports_size(self, store: ObstoreBlobStore) -> None:
        await store.put("a/b.bin", b"12345")
        assert (await store.head("a/b.bin")).size == 5

    async def test_missing_key_raises(self, store: ObstoreBlobStore) -> None:
        with pytest.raises(ObjectNotFoundError):
            await store.get("nope")

    async def test_exists(self, store: ObstoreBlobStore) -> None:
        assert not await store.exists("x")
        await store.put("x", b"1")
        assert await store.exists("x")

    async def test_delete_is_idempotent(self, store: ObstoreBlobStore) -> None:
        await store.put("x", b"1")
        await store.delete("x")
        await store.delete("x")  # must not raise
        assert not await store.exists("x")

    async def test_memory_cannot_sign(self, store: ObstoreBlobStore) -> None:
        with pytest.raises(StorageError):
            await store.signed_url("x")


class TestUriParsing:
    def test_schemes(self, tmp_path: Path) -> None:
        assert store_from_uri("memory://").scheme == "memory"
        assert store_from_uri(f"file://{tmp_path}").scheme == "file"

    def test_bucket_and_prefix_split(self) -> None:
        s3 = store_from_uri("s3://my-bucket/media/prefix", region="us-east-1")
        assert s3.scheme == "s3"
        assert s3.bucket == "my-bucket"

    def test_gcs_uri(self) -> None:
        gs = store_from_uri("gs://cookiebot-media")
        assert gs.scheme == "gs"
        assert gs.bucket == "cookiebot-media"

    def test_unsupported_scheme(self) -> None:
        with pytest.raises(ValueError):
            store_from_uri("ftp://nope")

    def test_missing_bucket(self) -> None:
        with pytest.raises(ValueError):
            store_from_uri("s3://")


class TestLocalBackend:
    async def test_roundtrip_on_disk(self, tmp_path: Path) -> None:
        store = store_from_uri(f"file://{tmp_path}")
        await store.put("nested/dir/file.bin", b"payload")
        assert await store.get("nested/dir/file.bin") == b"payload"
