"""Blob storage contract.

One protocol, four backends (S3, GCS, local filesystem, memory). Handlers depend
on this interface only, so swapping GCS for S3 is an env-var change.

v1 had no abstraction at all: media went straight to a hardcoded GCS bucket via
`google-cloud-storage` with a service-account key path baked into
`universal_funcs.py:24`, and everything else was written to fixed filenames in
the process working directory (`meme.png`, `CAPTCHA.png`, `temp.jpg`), which the
50-thread pool raced on — see FEATURE-MAP D4.
"""

from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

import msgspec

#: Mirrors obstore's `HTTP_METHOD` (obstore._sign) — the only methods a backend
#: can sign a URL for.
SignMethod = Literal["GET", "PUT", "POST", "HEAD", "PATCH", "TRACE", "DELETE", "OPTIONS", "CONNECT"]


class ObjectMeta(msgspec.Struct, frozen=True):
    """What the backend knows about a stored object."""

    key: str
    size: int
    etag: str | None = None
    version: str | None = None
    content_type: str | None = None


class StorageError(RuntimeError):
    """Backend failure. Wraps provider-specific exceptions so callers stay portable."""


class ObjectNotFoundError(StorageError):
    """The key does not exist."""


@runtime_checkable
class BlobStore(Protocol):
    """Async object storage.

    Keys are `/`-separated paths without a leading slash. Implementations must be
    safe to share across tasks — one instance per process, not per request.
    """

    @property
    def scheme(self) -> str:
        """`s3` | `gs` | `file` | `memory` — used for logs and metrics labels."""
        ...

    async def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> ObjectMeta: ...

    async def get(self, key: str) -> bytes:
        """Raises ObjectNotFoundError if absent."""
        ...

    async def head(self, key: str) -> ObjectMeta:
        """Metadata without transferring the body. Raises ObjectNotFoundError if absent."""
        ...

    async def exists(self, key: str) -> bool: ...

    async def delete(self, key: str) -> None:
        """Idempotent — deleting a missing key is not an error."""
        ...

    async def signed_url(
        self, key: str, *, expires_in: int = 3600, method: SignMethod = "GET"
    ) -> str:
        """Pre-signed URL so Telegram (or a browser) can fetch without our credentials.

        Backends that cannot sign (memory, local) raise StorageError.
        """
        ...

    async def close(self) -> None: ...
