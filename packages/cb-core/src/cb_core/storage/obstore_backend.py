"""BlobStore over `obstore` — Rust `object_store` bindings.

One dependency covers GCP and S3 (plus Azure, local and in-memory), so there is
no second code path to keep in sync and no per-cloud SDK to age out. Credentials
resolve from the ambient environment the same way each cloud's own SDK does:
`AWS_*` / instance role for S3, `GOOGLE_APPLICATION_CREDENTIALS` / workload
identity for GCS.
"""

from __future__ import annotations

import time
from datetime import timedelta
from types import TracebackType
from typing import Any, Literal

import obstore
from obstore import exceptions as obs_exc
from obstore.store import GCSStore, LocalStore, MemoryStore, S3Store

from cb_core import metrics
from cb_core.logging import get_logger
from cb_core.storage.base import (
    BlobStore,
    ObjectMeta,
    ObjectNotFoundError,
    SignMethod,
    StorageError,
)

log = get_logger("cb.storage")

# Only these can produce pre-signed URLs; local/memory raise instead of pretending.
_SIGNABLE = (S3Store, GCSStore)

# Only object stores carry per-object attributes. LocalStore and MemoryStore
# reject them with NotImplementedError, so dev and test runs skip that argument.
_ATTRIBUTE_CAPABLE = (S3Store, GCSStore)

# obstore surfaces a missing object as the stdlib FileNotFoundError, not as
# obstore.exceptions.NotFoundError — catch both so a future release that swaps
# them does not silently turn a 404 into a 500.
_NOT_FOUND = (FileNotFoundError, obs_exc.NotFoundError)
# Everything else the Rust layer can raise: its own hierarchy plus OSError, which
# is what the local backend maps IO failures to.
_BACKEND_ERRORS = (obs_exc.BaseError, OSError)


class ObstoreBlobStore(BlobStore):
    def __init__(self, store: Any, scheme: str, bucket: str | None = None) -> None:
        # `Any`, not obstore.store.ObjectStore: close() reassigns this to None to
        # release the Rust-side client pool, and every accessor below only ever
        # runs while the store is still live, so a precise Optional would need a
        # lifecycle-narrowing guard on every method purely to satisfy the checker.
        self._store = store
        self._scheme = scheme
        self._bucket = bucket

    @property
    def scheme(self) -> str:
        return self._scheme

    @property
    def bucket(self) -> str | None:
        return self._bucket

    async def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> ObjectMeta:
        attributes: dict[str, str] | None = None
        if isinstance(self._store, _ATTRIBUTE_CAPABLE):
            attributes = dict(metadata or {})
            if content_type:
                attributes["Content-Type"] = content_type
            # Blobs are content-addressed, so the body for a key never changes —
            # cache them for a year and let the CDN do the work.
            attributes.setdefault("Cache-Control", "public, max-age=31536000, immutable")

        with self._timed("put"):
            try:
                result = await obstore.put_async(
                    self._store, key, data, attributes=attributes or None
                )
            except _BACKEND_ERRORS as exc:
                raise StorageError(f"put {key!r} failed: {exc}") from exc

        return ObjectMeta(
            key=key,
            size=len(data),
            etag=result.get("e_tag"),
            version=result.get("version"),
            content_type=content_type,
        )

    async def get(self, key: str) -> bytes:
        with self._timed("get"):
            try:
                result = await obstore.get_async(self._store, key)
                return bytes(await result.bytes_async())
            except _NOT_FOUND as exc:
                raise ObjectNotFoundError(key) from exc
            except _BACKEND_ERRORS as exc:
                raise StorageError(f"get {key!r} failed: {exc}") from exc

    async def head(self, key: str) -> ObjectMeta:
        with self._timed("head"):
            try:
                meta = await obstore.head_async(self._store, key)
            except _NOT_FOUND as exc:
                raise ObjectNotFoundError(key) from exc
            except _BACKEND_ERRORS as exc:
                raise StorageError(f"head {key!r} failed: {exc}") from exc
        return ObjectMeta(
            key=key,
            size=int(meta["size"]),
            etag=meta.get("e_tag"),
            version=meta.get("version"),
        )

    async def exists(self, key: str) -> bool:
        try:
            await self.head(key)
        except ObjectNotFoundError:
            return False
        return True

    async def delete(self, key: str) -> None:
        with self._timed("delete"):
            try:
                await obstore.delete_async(self._store, key)
            except _NOT_FOUND:
                return  # idempotent by contract
            except _BACKEND_ERRORS as exc:
                raise StorageError(f"delete {key!r} failed: {exc}") from exc

    async def signed_url(
        self, key: str, *, expires_in: int = 3600, method: SignMethod = "GET"
    ) -> str:
        if not isinstance(self._store, _SIGNABLE):
            raise StorageError(f"{self._scheme} backend cannot sign URLs")
        with self._timed("sign"):
            try:
                return await obstore.sign_async(
                    self._store, method, key, timedelta(seconds=expires_in)
                )
            except _BACKEND_ERRORS as exc:
                raise StorageError(f"sign {key!r} failed: {exc}") from exc

    async def close(self) -> None:
        # obstore stores hold a Rust-side client pool released on drop.
        self._store = None

    def _timed(self, op: str) -> _OpTimer:
        return _OpTimer(self._scheme, op)


class _OpTimer:
    """Times a backend call and records outcome. Kept explicit so `raise` still propagates."""

    __slots__ = ("_op", "_scheme", "_start")

    def __init__(self, scheme: str, op: str) -> None:
        self._scheme = scheme
        self._op = op
        self._start = 0.0

    def __enter__(self) -> _OpTimer:
        self._start = time.perf_counter()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> Literal[False]:
        outcome = "ok" if exc_type is None else "error"
        metrics.storage_duration.labels(
            backend=self._scheme, operation=self._op, outcome=outcome
        ).observe(time.perf_counter() - self._start)
        if exc_type is not None:
            metrics.storage_errors_total.labels(backend=self._scheme, operation=self._op).inc()
        return False


def store_from_uri(uri: str, **options: str) -> ObstoreBlobStore:
    """Build a backend from a URI.

        s3://bucket/optional/prefix
        gs://bucket/optional/prefix
        file:///var/lib/cookiebot/media
        memory://

    Extra keyword options are passed through to the underlying store (region,
    endpoint, anonymous access, …) so a MinIO or fake-GCS endpoint works in CI.
    """
    if uri == "memory://" or uri.startswith("memory://"):
        return ObstoreBlobStore(MemoryStore(), "memory")

    if uri.startswith("file://"):
        path = uri[len("file://") :] or "."
        return ObstoreBlobStore(LocalStore(path, mkdir=True), "file")

    if uri.startswith("s3://"):
        bucket, prefix = _split_bucket(uri[len("s3://") :])
        # obstore's generated stubs type this as `Unpack[S3Config]`, a TypedDict of
        # specific keys; **options is a plain caller-supplied str dict (region,
        # endpoint, ...), which the stub has no way to match structurally.
        return ObstoreBlobStore(
            S3Store(bucket, prefix=prefix or None, **options),  # type: ignore[arg-type]
            "s3",
            bucket,
        )

    if uri.startswith("gs://"):
        bucket, prefix = _split_bucket(uri[len("gs://") :])
        return ObstoreBlobStore(
            GCSStore(bucket, prefix=prefix or None, **options),  # type: ignore[arg-type]
            "gs",
            bucket,
        )

    raise ValueError(
        f"unsupported storage URI {uri!r} (expected s3://, gs://, file:// or memory://)"
    )


def _split_bucket(rest: str) -> tuple[str, str]:
    bucket, _, prefix = rest.partition("/")
    if not bucket:
        raise ValueError("storage URI is missing a bucket name")
    return bucket, prefix.strip("/")
