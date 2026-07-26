"""UUIDv7 identifiers.

v7 embeds a millisecond timestamp in the high bits, so IDs sort by creation time.
That matters here for three reasons:

* **B-tree locality.** v4 inserts scatter across the whole index; v7 appends to
  the right edge, so index pages stay hot and write amplification drops.
* **Free ordering.** `ORDER BY id` is chronological — no second timestamp index
  for "most recent N" queries.
* **Citus-friendly.** IDs are generated in the application, so no shared sequence
  and no coordinator round trip on insert. The shard key stays `group_id`; the
  UUID is only the row's local identity.

Generated with `uuid_utils` (Rust). Postgres has a matching `cb_uuid_v7()` for
the rare server-side default — see migration 0002.
"""

from __future__ import annotations

import uuid as _stdlib_uuid

import uuid_utils


def uuid7() -> _stdlib_uuid.UUID:
    """A new v7 UUID as a stdlib UUID (what asyncpg wants for a `uuid` column)."""
    return _stdlib_uuid.UUID(str(uuid_utils.uuid7()))


def uuid7_str() -> str:
    return str(uuid_utils.uuid7())


def timestamp_ms(value: _stdlib_uuid.UUID) -> int:
    """Creation time in epoch milliseconds, read back out of a v7 UUID.

    Raises ValueError for any other UUID version — silently returning garbage for
    a v4 would be worse than failing.
    """
    if value.version != 7:
        raise ValueError(f"expected a UUIDv7, got v{value.version}")
    return value.int >> 80


def is_uuid7(value: _stdlib_uuid.UUID) -> bool:
    return value.version == 7
