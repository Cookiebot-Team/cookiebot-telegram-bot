"""Key layout.

**Content-addressed blobs, per-group reference rows.**

The blob key is derived from the content hash alone, so the same sticker or meme
uploaded in fifty groups is stored once. Tenancy and deletion live in Postgres:
`media_objects` holds one row per (group, blob) with the group as the shard key,
so "this group left, drop its media" is a local DELETE, and the blob is garbage
collected when its last reference goes.

v1 stored a fresh copy per forwarded item with no dedupe at all
(`SocialContent.py:191-196`), which is why the random-media pool filled with
duplicates.

The two-character fan-out directory (`ab/`) keeps object listings usable on
backends that paginate lexicographically.
"""

from __future__ import annotations

from cb_core.dedupe import fingerprint

# Extension per media kind. Telegram gives us a MIME type we do not fully trust,
# so the kind (which we derive from the update) picks the extension.
_EXTENSIONS: dict[str, str] = {
    "photo": ".jpg",
    "sticker": ".webp",
    "animation": ".mp4",
    "video": ".mp4",
    "voice": ".ogg",
    "audio": ".mp3",
    "document": ".bin",
    "card": ".png",
    "captcha": ".png",
}

VALID_KINDS: frozenset[str] = frozenset(_EXTENSIONS)


def extension_for(kind: str) -> str:
    return _EXTENSIONS.get(kind, ".bin")


def blob_key(kind: str, content_hash: str) -> str:
    """`media/<kind>/<hh>/<hash><ext>` — stable, immutable, cacheable forever."""
    if kind not in _EXTENSIONS:
        raise ValueError(f"unknown media kind {kind!r}")
    if len(content_hash) < 4:
        raise ValueError("content hash too short")
    return f"media/{kind}/{content_hash[:2]}/{content_hash}{extension_for(kind)}"


def hash_and_key(kind: str, data: bytes) -> tuple[str, str]:
    """blake3 the bytes and derive the key in one pass."""
    digest = fingerprint(data)
    return digest, blob_key(kind, digest)


def derived_key(source_hash: str, variant: str, ext: str = ".png") -> str:
    """Key for something we generated from a source blob (thumbnail, distorted copy).

    Deterministic, so regenerating the same variant overwrites rather than
    accumulating — v1 left `distorted.jpg` debris that startup had to sweep
    (`COOKIEBOT.py:27-29`).
    """
    if not variant.isidentifier():
        raise ValueError(f"variant must be an identifier, got {variant!r}")
    return f"derived/{variant}/{source_hash[:2]}/{source_hash}{ext}"
