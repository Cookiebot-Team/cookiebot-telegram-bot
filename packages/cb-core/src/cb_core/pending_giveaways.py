"""The prize an admin typed, waiting for them to pick a winner count.

v1 carried it in the callback data itself: `giveaways_ask` built
`f'GIVEAWAY {n} {prize}'` where `prize` was `json.dumps(prize_text)[:20]`
(`../COOKIEBOT-Telegram-Group-Bot/Bot/Giveaways.py:36,41-45`), and the press
handler reconstructed it with `query_data.replace('"', '').split()[2:]`
(`COOKIEBOT.py:421`). Three things follow from that, and none of them survive
here:

* Telegram caps `callback_data` at **64 bytes**, which is why v1 truncated to
  20 characters — so a prize longer than that was silently cut in the
  announcement, and one containing a `"` came back mangled.
* The round trip is `json.dumps` on the way out and `json.loads` on the way
  back (`:54`) with the quotes stripped in between, so `json.loads` was handed
  an unquoted bare word and raised for every ordinary prize. That is D-GA-1 in
  `docs/contracts/x_giveaways.md`: v1's `/giveaway` never completed at all.
* Nothing shared it between processes, so only the replica that sent the
  prompt could have answered the press.

Same answer `cb_core.pending_posts` already gives for the publisher's
submitted-but-unapproved post: the prize lives in Valkey and the callback data
carries only a token that points at it (`GIVEAWAY <n> <token>`) — the same
shape as v1's `GIVEAWAY <n> <prize>`, with a fixed-width opaque id where v1
put user text.
"""

from __future__ import annotations

from cb_core import cache
from cb_core.ids import uuid7_str
from cb_core.logging import get_logger

log = get_logger("cb.pending_giveaways")

_PREFIX = "giveaway:pending:"

#: An admin who opens the prompt and never presses a button. Long enough that
#: a distracted admin still finds it live, short enough that an abandoned
#: prompt is not answerable a day later.
TTL_SECONDS = 3600


def new_token() -> str:
    """The opaque handle that goes in the callback data. UUIDv7 like every
    other surrogate id here (AGENTS.md §2.3); 36 characters leaves the whole
    payload at 47 bytes, well inside Telegram's 64-byte `callback_data` cap."""
    return uuid7_str()


def key_for(token: str) -> str:
    return f"{_PREFIX}{token}"


async def put(token: str, prize: str) -> None:
    """Remember `prize` against the token its keyboard carries.

    A cache outage loses the draft, logged and not raised: the admin has
    already been shown the keyboard, and the press then answers
    `giveaway.not_found` — the same string v1 uses for every "this raffle is
    gone" case.
    """
    try:
        await cache.client().set(key_for(token), prize, ex=TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001 - see docstring
        log.warning("giveaway.pending_write_failed", error=str(exc))


async def take(token: str) -> str | None:
    """Read and remove. The prompt only ever produces one giveaway."""
    key = key_for(token)
    try:
        raw = await cache.client().get(key)
    except Exception as exc:  # noqa: BLE001 - a cache outage is a miss, not an error
        log.warning("giveaway.pending_read_failed", error=str(exc))
        return None
    try:
        await cache.delete(key)
    except Exception as exc:  # noqa: BLE001 - nothing downstream depends on this
        log.warning("giveaway.pending_discard_failed", error=str(exc))
    if raw is None:
        return None
    return raw.decode() if isinstance(raw, bytes) else str(raw)


__all__ = ["TTL_SECONDS", "key_for", "new_token", "put", "take"]
