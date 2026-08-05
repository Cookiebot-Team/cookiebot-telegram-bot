"""The submitted-but-not-yet-approved post, shared across replicas.

v1 held this in `cache_posts`, a module-global dict keyed by
`str(forward_from_message_id)` (`../COOKIEBOT-Telegram-Group-Bot/Bot/Publisher.py:19`),
written by `add_post_to_cache` (`:26-44`) and read once by `prepare_post`
(`:183`). Two consequences v2 cannot inherit: a restart between the submission
and the approval silently lost the post, and — worse here than in v1, which ran
one publisher process — a replica that did not handle the submission cannot see
it at all. That is FEATURE-MAP D6's shape, and this module is the same answer
`cb_core.cache` already gives everywhere else.

The keyspace is v1's, deliberately: `forward_from_message_id` alone, so two
groups forwarding the same channel post still share one entry and the second
submission still overwrites the first. That is observable (the earlier
submitter's approval renders the later submitter's caption) and it is what v1
does.
"""

from __future__ import annotations

import msgspec

from cb_core import cache
from cb_core.logging import get_logger
from cb_core.settings import get_settings

log = get_logger("cb.pending_posts")

_PREFIX = "publisher:pending:"


class PendingPost(msgspec.Struct, frozen=True):
    """What `prepare_post` needs and nothing else.

    `media_kind` is one of `photo`/`video`/`animation` — v1's own three send
    branches (`:210-218`). A document is stored as `animation`, because v1's
    resolver files it under that key (`:36-38`) and re-sends it with
    `sendAnimation`; these ads are GIFs, which is what makes that work.

    Only the *URLs* of the caption entities are kept: `prepare_post` reads
    nothing else off them (`:193-196`).
    """

    media_kind: str
    file_id: str
    caption: str
    caption_entity_urls: tuple[str, ...] = ()


_encoder = msgspec.msgpack.Encoder()
_decoder = msgspec.msgpack.Decoder(PendingPost)


def key_for(forward_from_message_id: int | str) -> str:
    return f"{_PREFIX}{forward_from_message_id}"


async def put(forward_from_message_id: int | str, post: PendingPost) -> None:
    """Store the pending post. A cache outage loses the submission, logged.

    v1's dict write could not fail; this can, and swallowing it is right: the
    caller has already told the user the post went for approval, and raising
    here would turn a Valkey blip into a handler error the user sees twice.
    The approval press then finds nothing and answers `publish_expired`.
    """
    try:
        await cache.client().set(
            key_for(forward_from_message_id),
            _encoder.encode(post),
            ex=get_settings().publisher_pending_ttl_seconds,
        )
    except Exception as exc:  # noqa: BLE001 - see docstring
        log.warning("publisher.pending_write_failed", error=str(exc))


async def get(forward_from_message_id: int | str) -> PendingPost | None:
    try:
        raw = await cache.client().get(key_for(forward_from_message_id))
    except Exception as exc:  # noqa: BLE001 - a cache outage is a miss, not an error
        log.warning("publisher.pending_read_failed", error=str(exc))
        return None
    if raw is None:
        return None
    try:
        return _decoder.decode(raw)
    except msgspec.DecodeError as exc:
        # A payload written by an older struct shape. Treat as absent rather
        # than crashing the approval job; the submitter can resubmit.
        log.warning("publisher.pending_decode_failed", error=str(exc))
        return None


async def take(forward_from_message_id: int | str) -> PendingPost | None:
    """Read and remove, matching v1's `cache_posts.pop` (`:219-220`).

    The delete is best-effort and runs even when the read missed, so a decode
    failure does not leave an undecodable payload sitting until its TTL.
    """
    post = await get(forward_from_message_id)
    await discard(forward_from_message_id)
    return post


async def discard(forward_from_message_id: int | str) -> None:
    """v1's `deny_post` (`:223-228`) — drop the entry, answer nothing."""
    try:
        await cache.delete(key_for(forward_from_message_id))
    except Exception as exc:  # noqa: BLE001 - nothing downstream depends on this succeeding
        log.warning("publisher.pending_discard_failed", error=str(exc))
