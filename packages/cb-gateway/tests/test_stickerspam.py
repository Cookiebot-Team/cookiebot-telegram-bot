"""Unit coverage for core_stickerspam — pure logic only, no dispatcher, no Telegram, no DB.

See docs/contracts/core_stickerspam.md for the full behaviour contract and
qa/features/core_stickerspam.feature + qa/test_core_stickerspam.py for the
end-to-end version of the same assertions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock

import pytest

from cb_core import cache
from cb_gateway.handlers import stickerspam as stickerspam_handler


@dataclass
class _FakeConfig:
    sticker_spam_limit: int = 5
    sticker_spam_window_s: int = 60


@dataclass
class _FakeCtx:
    group_id: int
    config: _FakeConfig
    lang: str = "en"
    is_admin: bool = False


@dataclass
class _FakeChat:
    id: int


@dataclass
class _FakeMessage:
    """Only the attributes the handler actually reads."""

    chat: _FakeChat
    message_id: int = 1
    bot: Any = None
    reply: Any = None


def _message(group_id: int = -100, message_id: int = 42) -> _FakeMessage:
    message = _FakeMessage(chat=_FakeChat(id=group_id), message_id=message_id)
    message.reply = AsyncMock()
    message.bot = type("Bot", (), {"delete_message": AsyncMock()})()
    return message


# ---------------------------------------------------------------- key derivation


class TestKey:
    def test_key_is_scoped_to_the_group_only(self) -> None:
        """v1 keyed `last_used_sticker` by `chat_id` alone (`Cooldowns.py:8`) —
        one user's stickers count toward the whole group's total, and the key
        must carry no user id to match."""
        assert stickerspam_handler._key(-100) == "cb:stickerspam:-100"  # noqa: SLF001

    def test_different_groups_get_different_keys(self) -> None:
        assert stickerspam_handler._key(-100) != stickerspam_handler._key(-200)  # noqa: SLF001


# --------------------------------------------------------------- window maths


class FakeWindowCache:
    """Same fixed-window semantics as `cb_core.cache.incr_window`: atomic
    increment, expiry set only on the first increment of a key. A real Valkey
    round trip can't be driven through a window rollover without a live server
    and real wall-clock time, so this fake — driven by an explicit `now` — pins
    the rollover math directly, which is the one deliberate behaviour change
    from v1 (see stickerspam.py's module docstring, FEATURE-MAP D6).
    """

    def __init__(self) -> None:
        self._store: dict[str, tuple[float, int]] = {}
        self.now = 0.0

    async def incr_window(self, key: str, window_seconds: int) -> int:
        expires_at, count = self._store.get(key, (0.0, 0))
        if self.now >= expires_at:
            count = 0
            expires_at = self.now + window_seconds
        count += 1
        self._store[key] = (expires_at, count)
        return count


@pytest.mark.asyncio
async def test_bump_counts_up_within_the_window(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeWindowCache()
    monkeypatch.setattr(cache, "incr_window", fake.incr_window)

    counts = [await stickerspam_handler._bump(-100, window_seconds=60) for _ in range(5)]  # noqa: SLF001

    assert counts == [1, 2, 3, 4, 5]


@pytest.mark.asyncio
async def test_bump_resets_after_the_window_elapses(monkeypatch: pytest.MonkeyPatch) -> None:
    """The deliberate fix for FEATURE-MAP D6: unlike v1's dict, which never
    reset on its own within a process (`Cooldowns.py:8-22` — only an unrelated
    *non-sticker* message happening to arrive reset it, never a timeout, and
    never across the five v1 processes or a restart), the shared counter is
    bounded by `sticker_spam_window_s` and starts over at 1 once the window
    elapses, with no dependency on other traffic in the chat."""
    fake = FakeWindowCache()
    monkeypatch.setattr(cache, "incr_window", fake.incr_window)

    for _ in range(5):
        await stickerspam_handler._bump(-100, window_seconds=60)  # noqa: SLF001
    fake.now = 61.0  # window elapsed
    count = await stickerspam_handler._bump(-100, window_seconds=60)  # noqa: SLF001

    assert count == 1


@pytest.mark.asyncio
async def test_bump_is_independent_per_group(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeWindowCache()
    monkeypatch.setattr(cache, "incr_window", fake.incr_window)

    await stickerspam_handler._bump(-100, window_seconds=60)  # noqa: SLF001
    await stickerspam_handler._bump(-100, window_seconds=60)  # noqa: SLF001
    count_other_group = await stickerspam_handler._bump(-200, window_seconds=60)  # noqa: SLF001

    assert count_other_group == 1


@pytest.mark.asyncio
async def test_bump_fails_open_when_the_cache_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Valkey outage must be reported as "cannot tell", never as "assume over
    the limit" — this is the seam `sticker_anti_spam` relies on to fail open
    rather than delete every sticker in every group (docs/contracts/core_stickerspam.md,
    "cache outage")."""

    async def boom(key: str, window_seconds: int) -> int:
        raise RuntimeError("cache not initialised")

    monkeypatch.setattr(cache, "incr_window", boom)

    result = await stickerspam_handler._bump(-100, window_seconds=60)  # noqa: SLF001

    assert result is None


# ------------------------------------------------------------------- handler


@pytest.mark.asyncio
async def test_no_warning_or_deletion_below_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        stickerspam_handler, "context_for", AsyncMock(return_value=_FakeCtx(-100, _FakeConfig()))
    )
    monkeypatch.setattr(stickerspam_handler, "_bump", AsyncMock(return_value=4))

    message = _message()
    await stickerspam_handler.sticker_anti_spam(message)

    message.reply.assert_not_awaited()
    message.bot.delete_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_warns_exactly_at_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        stickerspam_handler, "context_for", AsyncMock(return_value=_FakeCtx(-100, _FakeConfig()))
    )
    monkeypatch.setattr(stickerspam_handler, "_bump", AsyncMock(return_value=5))

    message = _message()
    await stickerspam_handler.sticker_anti_spam(message)

    message.reply.assert_awaited_once()
    (sent_text,), _ = message.reply.await_args
    from cb_core import locales

    assert sent_text == locales.get("flood_stickers", "en")
    message.bot.delete_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_deletes_every_sticker_past_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        stickerspam_handler, "context_for", AsyncMock(return_value=_FakeCtx(-100, _FakeConfig()))
    )
    monkeypatch.setattr(stickerspam_handler, "_bump", AsyncMock(return_value=6))

    message = _message(group_id=-100, message_id=999)
    await stickerspam_handler.sticker_anti_spam(message)

    message.reply.assert_not_awaited()
    message.bot.delete_message.assert_awaited_once_with(-100, 999)


@pytest.mark.asyncio
async def test_no_action_at_all_when_the_cache_is_down(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_bump` returning `None` (cache unreachable) must short-circuit before
    any warn/delete decision — the fail-open contract end to end."""
    monkeypatch.setattr(
        stickerspam_handler, "context_for", AsyncMock(return_value=_FakeCtx(-100, _FakeConfig()))
    )
    monkeypatch.setattr(stickerspam_handler, "_bump", AsyncMock(return_value=None))

    message = _message()
    await stickerspam_handler.sticker_anti_spam(message)

    message.reply.assert_not_awaited()
    message.bot.delete_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_admins_are_not_exempt(monkeypatch: pytest.MonkeyPatch) -> None:
    """v1's call site (`COOKIEBOT.py:179-180`) has no admin check at all —
    preserved, not fixed, since it is a real observed v1 quirk rather than a
    silent-failure bug (AGENTS.md Phase 2: user-visible quirks are usually
    kept)."""
    monkeypatch.setattr(
        stickerspam_handler,
        "context_for",
        AsyncMock(return_value=_FakeCtx(-100, _FakeConfig(), is_admin=True)),
    )
    monkeypatch.setattr(stickerspam_handler, "_bump", AsyncMock(return_value=5))

    message = _message()
    await stickerspam_handler.sticker_anti_spam(message)

    message.reply.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_failed_delete_does_not_propagate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mirrors v1's `delete_message`, which swallows its own exception and
    prints instead of raising (`universal_funcs.py:340-344`) — a message
    already gone (deleted by an admin, migrated group) must not break the
    update."""
    monkeypatch.setattr(
        stickerspam_handler, "context_for", AsyncMock(return_value=_FakeCtx(-100, _FakeConfig()))
    )
    monkeypatch.setattr(stickerspam_handler, "_bump", AsyncMock(return_value=6))

    message = _message()
    message.bot.delete_message = AsyncMock(side_effect=RuntimeError("message to delete not found"))

    await stickerspam_handler.sticker_anti_spam(message)  # must not raise
