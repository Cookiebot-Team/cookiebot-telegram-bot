"""Blob storage: one interface, GCS and S3 first, local and memory for dev/tests."""

from __future__ import annotations

from cb_core.settings import Settings
from cb_core.storage.base import BlobStore, ObjectMeta, ObjectNotFoundError, StorageError
from cb_core.storage.keys import blob_key, derived_key, hash_and_key
from cb_core.storage.media import MediaRef, MediaService
from cb_core.storage.obstore_backend import ObstoreBlobStore, store_from_uri

__all__ = [
    "BlobStore",
    "MediaRef",
    "MediaService",
    "ObjectMeta",
    "ObjectNotFoundError",
    "ObstoreBlobStore",
    "StorageError",
    "blob_key",
    "close_storage",
    "derived_key",
    "hash_and_key",
    "init_storage",
    "media",
    "store",
    "store_from_uri",
]

_store: BlobStore | None = None
_media: MediaService | None = None


async def init_storage(settings: Settings) -> BlobStore:
    global _store, _media
    if _store is None:
        _store = store_from_uri(settings.storage_uri)
        _media = MediaService(_store)
    return _store


async def close_storage() -> None:
    global _store, _media
    if _store is not None:
        await _store.close()
        _store = None
        _media = None


def store() -> BlobStore:
    if _store is None:
        raise RuntimeError("storage not initialised; call init_storage() during startup")
    return _store


def media() -> MediaService:
    if _media is None:
        raise RuntimeError("storage not initialised; call init_storage() during startup")
    return _media
