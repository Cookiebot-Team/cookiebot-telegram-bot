"""Read-only access to v1's private GCS bucket — the migration's one and only
read path into it.

**The source bucket must never be written to, and that is enforced three ways,
not just documented:**

1. The client is built with credentials minted for exactly one OAuth scope,
   `https://www.googleapis.com/auth/devstorage.read_only` — see `_READ_ONLY_SCOPE`
   and `open_source` below. `google.cloud.storage.Client`'s own default (`Client.SCOPE`)
   offers `read_only`, `read_write` *and* `full_control` and picks whichever the
   credentials happen to support; asking `google.auth.default()` for the
   read-only scope explicitly means the token Google hands back is incapable of
   a write call before the request ever reaches this bucket's IAM policy — a
   `blob.upload_from_*()` or `.delete()` call would 403 at Google's API layer,
   not merely at ours.
2. `GcsReadOnlySource` exposes exactly two methods, `list_prefix` and
   `download`. No delete, no upload, no patch, and the underlying
   `google.cloud.storage.Bucket`/`Client` handles are private attributes never
   returned to a caller — there is no way to reach a write-capable method
   through this object even if the scope above were somehow bypassed.
3. `tests/test_bucket_export.py::TestReadOnlyEnforcement` asserts both of the
   above mechanically (the method set, and the scope requested of
   `google.auth.default`) so a future edit that adds a write path fails the
   suite, not just review.

This is the *source* side of the migration; the destination goes through
`cb_core.storage` like the rest of the app (see `bucket_export/__init__.py`).
Reading a foreign, soon-to-be-decommissioned system with its native SDK
directly is the same call `cb_worker.importer.source` already made for v1's
MongoDB (`pymongo` there, `google-cloud-storage` here) — AGENTS.md §5's
"never touch a cloud SDK directly" governs how *our own* storage is written,
not how a legacy source outside our control is read once, on the way out.

**Where the credential comes from** is `gcp_auth.export_credentials` (see
that module), not a bare `google.auth.default()` call — `open_source` below
still asks for exactly `_READ_ONLY_SCOPE`, but the credential behind that
scope may now be an impersonated, temporary service account
(`gcs-auth provision`) rather than the operator's own token, and this module
does not need to know or care which: `export_credentials` guarantees the
scope either way, which is the property this module's docstring is actually
about.
"""

from __future__ import annotations

from collections.abc import Iterator

from google.api_core.exceptions import GoogleAPIError
from google.cloud import storage

from cb_core.logging import get_logger
from cb_worker.bucket_export import SourceBlob, gcp_auth

log = get_logger("cb.bucket_export.source")

#: See module docstring point 1. Read-only, full stop — no `read_write` or
#: `full_control` fallback under any code path in this module. Re-exported
#: from `gcp_auth` rather than redefined so there is exactly one literal
#: scope string in this package, not two that could drift apart.
_READ_ONLY_SCOPE = gcp_auth.READ_ONLY_SCOPE


class GcsSourceError(RuntimeError):
    """A source-layer failure with a message that is safe to log and show."""


class GcsReadOnlySource:
    """Wraps a v1 GCS bucket down to `list_prefix` and `download` — nothing else.

    Constructed only by `open_source`, which is what actually requests the
    read-only-scoped credentials; this class does not know or care how its
    `Client` was built, so a test can hand it a client backed by any
    credentials (or a fake) without touching real GCS.
    """

    def __init__(self, client: storage.Client, bucket_name: str) -> None:
        self._bucket_name = bucket_name
        # Private: never exposed via a property or method, per module docstring
        # point 2. A caller that wants bytes goes through `download`.
        self._bucket = client.bucket(bucket_name)

    def list_prefix(self, prefix: str) -> Iterator[SourceBlob]:
        """Every blob under `prefix`, metadata only — the listing API never
        transfers a body, so this is cheap even for a large prefix."""
        try:
            for blob in self._bucket.list_blobs(prefix=prefix):
                yield SourceBlob(
                    name=blob.name,
                    size=blob.size or 0,
                    updated=blob.updated,
                    md5_hash=blob.md5_hash,
                )
        except GoogleAPIError as exc:
            raise GcsSourceError(
                f"listing {self._bucket_name!r} prefix {prefix!r} failed: "
                f"{gcp_auth.diagnose_google_error(exc)}"
            ) from exc

    def download(self, name: str) -> bytes:
        """The full bytes of one blob."""
        try:
            return self._bucket.blob(name).download_as_bytes()
        except GoogleAPIError as exc:
            raise GcsSourceError(
                f"downloading {self._bucket_name!r}/{name} failed: {gcp_auth.diagnose_google_error(exc)}"
            ) from exc

    def close(self) -> None:
        self._bucket.client.close()


def open_source(bucket_name: str) -> GcsReadOnlySource:
    """Build the read-only source client.

    The credential comes from `gcp_auth.export_credentials`: a temporary,
    impersonated service account provisioned by `gcs-auth provision` if
    `CB_GCS_EXPORT_SERVICE_ACCOUNT` names one (the preferred path — no key
    ever touches disk), a service-account key at `GOOGLE_APPLICATION_CREDENTIALS`
    if that is set instead, or the operator's own Application Default
    Credentials otherwise — every one of those re-scoped to read-only, see
    module docstring point 1. A missing/unusable credential fails here with an
    actionable message instead of the bare `DefaultCredentialsError` traceback
    `google.auth` raises, which names an environment variable but not what to
    do about it — and now names `gcs-auth provision` as the way to stop
    depending on that variable at all.
    """
    if not bucket_name:
        raise ValueError(
            "no source bucket configured — set CB_BUCKET_EXPORT_SOURCE_BUCKET "
            "to v1's private bucket name (e.g. 'cookiebot-bucket')"
        )
    try:
        credentials, project = gcp_auth.export_credentials()
    except gcp_auth.GcsAuthError as exc:
        raise GcsSourceError(
            f"no usable Google credentials for the source bucket: {exc} The preferred fix is "
            f"`python scripts/cb.py gcs-auth provision --bucket {bucket_name}`, which provisions "
            "a temporary, impersonated service account and never writes a key to disk. "
            "Alternatively, set GOOGLE_APPLICATION_CREDENTIALS to a service-account key that has "
            "at least storage.objectViewer on the bucket."
        ) from exc
    client = storage.Client(credentials=credentials, project=project)
    log.info("bucket_export.source.opened", bucket=bucket_name, scope=_READ_ONLY_SCOPE)
    return GcsReadOnlySource(client, bucket_name)


__all__ = ["GcsReadOnlySource", "GcsSourceError", "open_source"]
