"""Update deduplication and media fingerprinting — hot path, Cython-compiled.

Telegram redelivers webhook updates on any non-2xx, so dedupe is mandatory.
v1 kept two unbounded sets and, at 10 000 entries, called `.clear()` — dropping
every id at once, which reopens the duplicate window right after a burst
(`COOKIEBOT.py:44-46`). This is a real LRU: eviction is oldest-first, so the
recent window is never lost.
"""

from __future__ import annotations

import cython
from blake3 import blake3

COMPILED: bool = cython.compiled


@cython.cclass
class RecentIds:
    """Fixed-capacity LRU of seen ids. `seen()` is O(1) and allocation-free in steady state."""

    capacity: cython.Py_ssize_t
    _ids: set
    _order: list
    _head: cython.Py_ssize_t

    def __init__(self, capacity: int = 65536) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self.capacity = capacity
        self._ids = set()
        self._order = [0] * capacity
        self._head = 0

    @cython.ccall
    def seen(self, update_id: cython.longlong) -> cython.bint:
        """True if this id was already recorded. Records it as a side effect."""
        ids: set = self._ids
        order: list = self._order
        head: cython.Py_ssize_t = self._head
        if update_id in ids:
            return True
        if len(ids) >= self.capacity:
            ids.discard(order[head])
        order[head] = update_id
        ids.add(update_id)
        head += 1
        self._head = 0 if head >= self.capacity else head
        return False

    def __len__(self) -> int:
        return len(self._ids)

    def __contains__(self, update_id: int) -> bool:
        return update_id in self._ids


def fingerprint(data: bytes) -> str:
    """blake3 content hash — dedupes media before it reaches the random/sticker DB.

    v1 stored every forwarded photo/video reference unconditionally
    (`SocialContent.py:191-196`), so the random pool filled with duplicates.
    """
    return blake3(data).hexdigest(length=16)


def fingerprint_parts(*parts: str) -> str:
    """Stable hash of identifying strings (file_unique_id, chat, sticker set...)."""
    # hot-types: ignore  blake3's hasher is a Rust extension object; Cython cannot
    # lower it to a C type, so an annotation here would satisfy the audit without
    # removing a single interpreter round trip.
    h = blake3()
    for p in parts:
        h.update(p.encode())
        h.update(b"\x1f")
    return h.hexdigest(length=16)


def idempotency_key(bot: str, update_id: int) -> str:
    """Cross-replica dedupe key. Gateway replicas share it via Valkey SET NX."""
    return "cb:upd:" + bot + ":" + str(update_id)
