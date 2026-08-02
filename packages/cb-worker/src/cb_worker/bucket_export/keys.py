"""Destination key derivation for exported blobs.

Content-hash-addressed, same idea as `cb_core.storage.keys.blob_key` (blake3,
two-hex-char fan-out) but under its own `legacy/v1-bucket/` namespace rather
than `media/<kind>/...` — these blobs have no `media` `kind` (VALID_KINDS is a
Telegram-update vocabulary: photo/sticker/animation/... this is a static-asset
export) and no group to scope a `media_objects` row to (see
`bucket_export/__init__.py`'s "Where the copies land"). A namespace of our own
avoids ever colliding with a real media key.

Content-addressing is also what makes the idempotency check free: the key IS
the hash, so "does the destination already have this content" is one
`store().exists(key)` call, no separate index to maintain.
"""

from __future__ import annotations

from posixpath import splitext


def destination_key(content_hash: str, source_name: str) -> str:
    """`legacy/v1-bucket/<hh>/<hash><ext>` — `ext` taken from the source blob's
    own name so the exported object is still openable/previewable by extension,
    same convention `cb_core.storage.keys.blob_key` uses for media."""
    if len(content_hash) < 4:
        raise ValueError("content hash too short")
    _, ext = splitext(source_name)
    return f"legacy/v1-bucket/{content_hash[:2]}/{content_hash}{ext}"


__all__ = ["destination_key"]
