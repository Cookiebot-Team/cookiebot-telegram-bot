"""Unit tests for storage — key derivation and the memory backend.

No infrastructure: the memory backend is the same code path as S3/GCS through
obstore, so the contract is exercised without a cloud or a container.
"""

from __future__ import annotations

from collections.abc import Mapping
from importlib import import_module
from pathlib import Path
from typing import Any

import pytest

from cb_core import tenancy as tenancy_mod
from cb_core.storage import MediaService, ObjectNotFoundError, StorageError, store_from_uri
from cb_core.storage.keys import blob_key, derived_key, extension_for, hash_and_key
from cb_core.storage.obstore_backend import ObstoreBlobStore
from cb_core.tenancy import Tenant

# `cb_core.storage`'s own `__init__.py` defines a `media()` accessor function
# and that name shadows the `cb_core.storage.media` submodule on the package
# object — `from cb_core.storage import media` would hand back the function,
# not the module `MediaService.put`'s `db`/`metrics` seams live on. Same
# situation `test_llm.py` documents for `cb_core.llm.router`; `import_module`
# goes straight to `sys.modules` instead of through the shadowed attribute.
media_mod = import_module("cb_core.storage.media")


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

    def test_no_prefix_matches_the_pre_tenancy_key_literally(self) -> None:
        """Regression guard: an omitted (or empty) `prefix` must reproduce
        exactly the key this module returned before `storage_prefix` had a
        reader — every tenant row today has `storage_prefix=""`
        (`0003_tenants.py`), and a changed empty-prefix key would strand every
        already-written `media_objects.blob_key`. Asserted against a literal
        string, not a re-derivation, so this cannot pass by construction."""
        assert blob_key("photo", "ab1234") == "media/photo/ab/ab1234.jpg"
        assert blob_key("photo", "ab1234", prefix="") == "media/photo/ab/ab1234.jpg"
        assert derived_key("abcdef123456", "thumbnail") == "derived/thumbnail/ab/abcdef123456.png"
        assert (
            derived_key("abcdef123456", "thumbnail", prefix="")
            == "derived/thumbnail/ab/abcdef123456.png"
        )

    def test_non_empty_prefix_is_prepended(self) -> None:
        assert blob_key("photo", "ab1234", prefix="acme") == "acme/media/photo/ab/ab1234.jpg"
        assert (
            derived_key("abcdef123456", "thumbnail", prefix="acme")
            == "acme/derived/thumbnail/ab/abcdef123456.png"
        )

    def test_prefix_breaks_cross_tenant_dedupe_on_purpose(self) -> None:
        """A per-tenant prefix is meant to give identical bytes two different
        keys across tenants — that is the isolation trade `storage_prefix`
        exists for (see `blob_key`'s docstring), not a bug."""
        _, unprefixed = hash_and_key("photo", b"same bytes")
        _, prefixed = hash_and_key("photo", b"same bytes", prefix="acme")
        assert prefixed == f"acme/{unprefixed}"
        assert prefixed != unprefixed


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


class _FakeMediaDb:
    """Enough of `cb_core.db`'s async surface for `MediaService.put`: the
    dedupe lookup (always a miss here) and the insert-returning row. No real
    Postgres — same "monkeypatch the module-level seam" convention
    `test_group_config.py` and `test_llm_budget.py` already use.
    """

    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(
        self, _query: str, *args: Any, name: str = "fetchrow"
    ) -> Mapping[str, Any] | None:
        if name == "media_by_hash":
            return None  # first write for this (group_id, content_hash): no existing row
        if name == "media_insert":
            media_id, _group_id, _kind, _content_hash, key, byte_size, content_type, *_rest = args
            return {
                "media_id": media_id,
                "blob_key": key,
                "byte_size": byte_size,
                "content_type": content_type,
                "existed": False,
            }
        raise AssertionError(f"unexpected fetchrow name={name!r}")

    async def execute(self, _query: str, *args: Any, name: str = "execute") -> str:
        self.executed.append((name, args))
        return "INSERT 0 1"


class TestMediaServiceStoragePrefix:
    """`MediaService.put`'s `tenant_id` parameter: resolves `Tenant.storage_prefix`
    once (`TenantRegistry.by_id`) and applies it to the derived key.
    """

    @pytest.fixture
    def service(self) -> MediaService:
        return MediaService(store_from_uri("memory://"))

    @pytest.fixture(autouse=True)
    def _fake_db(self, monkeypatch: pytest.MonkeyPatch) -> _FakeMediaDb:
        fake = _FakeMediaDb()
        monkeypatch.setattr(media_mod.db, "fetchrow", fake.fetchrow)
        monkeypatch.setattr(media_mod.db, "execute", fake.execute)
        return fake

    async def test_no_tenant_id_writes_the_unprefixed_key(self, service: MediaService) -> None:
        """The regression guard at the `MediaService` layer: omitting
        `tenant_id` — every caller before this parameter existed — must derive
        and store exactly the key `hash_and_key` returns with no prefix."""
        ref = await service.put(1, "photo", b"hello world")
        _, expected_key = hash_and_key("photo", b"hello world")
        assert ref.blob_key == expected_key
        assert await service.get_bytes(ref) == b"hello world"

    async def test_tenant_id_applies_the_tenants_prefix(
        self, service: MediaService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tenant = Tenant(tenant_id="acme", display_name="Acme", storage_prefix="acme")
        calls = 0

        async def fake_by_id(tenant_id: str) -> Tenant:
            nonlocal calls
            calls += 1
            assert tenant_id == "acme"
            return tenant

        monkeypatch.setattr(tenancy_mod.registry, "by_id", fake_by_id)

        ref = await service.put(1, "photo", b"hello world", tenant_id="acme")
        _, unprefixed = hash_and_key("photo", b"hello world")
        assert ref.blob_key == f"acme/{unprefixed}"
        assert calls == 1, "one registry lookup per put(), not one per key derived"

    async def test_stored_key_round_trips_through_the_store(
        self, service: MediaService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`media_objects.blob_key` (here, `MediaRef.blob_key`) is read back and
        used verbatim — `get_bytes` never re-derives a key from `content_hash` —
        so a prefixed write must resolve exactly like an unprefixed one."""
        tenant = Tenant(tenant_id="acme", display_name="Acme", storage_prefix="acme/v2")

        async def fake_by_id(tenant_id: str) -> Tenant:
            return tenant

        monkeypatch.setattr(tenancy_mod.registry, "by_id", fake_by_id)

        ref = await service.put(1, "sticker", b"a sticker's bytes", tenant_id="acme")
        assert ref.blob_key.startswith("acme/v2/media/sticker/")
        assert await service.get_bytes(ref) == b"a sticker's bytes"
