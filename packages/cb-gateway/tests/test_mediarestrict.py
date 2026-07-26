"""Unit coverage for core_mediarestrict — pure logic only, no dispatcher, no
Telegram, no DB.

See docs/contracts/core_mediarestrict.md for the full behaviour contract and
qa/features/core_mediarestrict.feature + qa/test_core_mediarestrict.py for the
end-to-end version of the same assertions. qa/integration/test_media_restrict.py
covers the real `group_members` round trip.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar
from unittest.mock import AsyncMock

import pytest
from aiogram.dispatcher.event.bases import SkipHandler

from cb_gateway.handlers import mediarestrict


@dataclass
class _FakeUser:
    id: int
    username: str | None = None
    first_name: str | None = None
    is_bot: bool = False


@dataclass
class _FakeMessage:
    """Only the attributes the handler actually reads."""

    from_user: _FakeUser | None = None
    new_chat_members: Any = None
    chat: Any = field(default_factory=lambda: type("Chat", (), {"id": -100})())
    bot: Any = None
    message_id: int = 42


class _FakeConfig:
    def __init__(self, media_restrict_seconds: int = 600) -> None:
        self.media_restrict_seconds = media_restrict_seconds


class _FakeCtx:
    def __init__(
        self, *, group_id: int = -100, is_admin: bool = False, media_restrict_seconds: int = 600
    ) -> None:
        self.group_id = group_id
        self.is_admin = is_admin
        self.lang = "en"
        self.config = _FakeConfig(media_restrict_seconds)


# --------------------------------------------------------- _is_within_restriction_window


class TestIsWithinRestrictionWindow:
    """GroupShield.py:145 — `if limbotimespan > 0`, plus the actual elapsed-time
    comparison v2's re-architecture introduces (see the contract's mechanism
    table)."""

    def test_just_joined_is_restricted(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=UTC)
        assert mediarestrict._is_within_restriction_window(now, 600, now=now)  # noqa: SLF001

    def test_joined_long_ago_is_not_restricted(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=UTC)
        joined_at = now - timedelta(seconds=601)
        assert not mediarestrict._is_within_restriction_window(joined_at, 600, now=now)  # noqa: SLF001

    def test_exactly_at_the_boundary_is_no_longer_restricted(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=UTC)
        joined_at = now - timedelta(seconds=600)
        assert not mediarestrict._is_within_restriction_window(joined_at, 600, now=now)  # noqa: SLF001

    def test_one_second_before_the_boundary_is_still_restricted(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=UTC)
        joined_at = now - timedelta(seconds=599)
        assert mediarestrict._is_within_restriction_window(joined_at, 600, now=now)  # noqa: SLF001

    def test_zero_seconds_means_the_feature_is_off(self) -> None:
        """v1's `if limbotimespan > 0` (GroupShield.py:145): 0 disables the
        feature entirely, even for a member who joined this instant."""
        now = datetime(2026, 1, 1, tzinfo=UTC)
        assert not mediarestrict._is_within_restriction_window(now, 0, now=now)  # noqa: SLF001

    def test_negative_seconds_also_means_off(self) -> None:
        """Defensive: v1 never produces a negative config value, but the
        handler's own `<= 0` guard and this pure function should agree."""
        now = datetime(2026, 1, 1, tzinfo=UTC)
        assert not mediarestrict._is_within_restriction_window(now, -5, now=now)  # noqa: SLF001


class TestRestrictMinutes:
    """GroupShield.py:149 — `round(limbotimespan/60)`."""

    def test_default_600_seconds_is_10_minutes(self) -> None:
        assert mediarestrict._restrict_minutes(600) == 10  # noqa: SLF001

    def test_rounds_rather_than_truncates(self) -> None:
        # 610 / 60 = 10.1666... -> rounds to 10; 630 / 60 = 10.5 -> banker's
        # rounding in Python rounds to 10 too (round-half-to-even) -- assert
        # the exact v1 arithmetic, not an idealised one.
        assert mediarestrict._restrict_minutes(610) == 10  # noqa: SLF001
        assert mediarestrict._restrict_minutes(650) == 11  # noqa: SLF001

    def test_ninety_seconds_rounds_up_to_two_minutes(self) -> None:
        assert mediarestrict._restrict_minutes(90) == 2  # noqa: SLF001


# --------------------------------------------------------------------- record_join


@pytest.mark.asyncio
async def test_record_join_skips_when_no_joiners(monkeypatch: pytest.MonkeyPatch) -> None:
    record = AsyncMock()
    monkeypatch.setattr(mediarestrict, "_record_join", record)

    message = _FakeMessage(new_chat_members=[])
    # record_join always yields so the rest of the join chain still runs.
    with pytest.raises(SkipHandler):
        await mediarestrict.record_join(message)

    record.assert_not_awaited()


@pytest.mark.asyncio
async def test_record_join_ignores_the_bot_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    record = AsyncMock()
    monkeypatch.setattr(mediarestrict, "_record_join", record)

    bot = type("Bot", (), {"id": 424242})()
    message = _FakeMessage(new_chat_members=[_FakeUser(id=424242)], bot=bot)
    # record_join always yields so the rest of the join chain still runs.
    with pytest.raises(SkipHandler):
        await mediarestrict.record_join(message)

    record.assert_not_awaited()


@pytest.mark.asyncio
async def test_record_join_ignores_another_bot_joining(monkeypatch: pytest.MonkeyPatch) -> None:
    record = AsyncMock()
    monkeypatch.setattr(mediarestrict, "_record_join", record)

    bot = type("Bot", (), {"id": 424242})()
    message = _FakeMessage(
        new_chat_members=[_FakeUser(id=99, is_bot=True)],
        bot=bot,
    )
    # record_join always yields so the rest of the join chain still runs.
    with pytest.raises(SkipHandler):
        await mediarestrict.record_join(message)

    record.assert_not_awaited()


@pytest.mark.asyncio
async def test_record_join_only_processes_the_first_joiner(monkeypatch: pytest.MonkeyPatch) -> None:
    """v1 quirk, preserved: `restrictChatMember` in `welcome_message` reads only
    the deprecated singular `new_chat_member` field — see the module docstring
    and docs/contracts/core_welcome.md's identical note about the same
    function's messaging half."""
    record = AsyncMock()
    monkeypatch.setattr(mediarestrict, "_record_join", record)

    bot = type("Bot", (), {"id": 424242})()
    first = _FakeUser(id=1)
    second = _FakeUser(id=2)
    message = _FakeMessage(new_chat_members=[first, second], bot=bot)
    # record_join always yields so the rest of the join chain still runs.
    with pytest.raises(SkipHandler):
        await mediarestrict.record_join(message)

    record.assert_awaited_once_with(-100, 1)


# ------------------------------------------------------------ enforce_media_restriction


@pytest.mark.asyncio
async def test_enforce_skips_when_no_from_user() -> None:
    message = _FakeMessage(from_user=None)
    # SkipHandler, not a quiet return: another router (core_stickerspam) may still
    # want this update — see handlers/__init__.py.
    with pytest.raises(SkipHandler):
        await mediarestrict.enforce_media_restriction(message)


@pytest.mark.asyncio
async def test_enforce_does_nothing_when_feature_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        mediarestrict,
        "context_for",
        AsyncMock(return_value=_FakeCtx(media_restrict_seconds=0)),
    )
    joined_at = AsyncMock()
    monkeypatch.setattr(mediarestrict, "_joined_at", joined_at)

    bot = type("Bot", (), {"delete_message": AsyncMock(), "send_message": AsyncMock()})()
    message = _FakeMessage(from_user=_FakeUser(id=1), bot=bot)
    with pytest.raises(SkipHandler):
        await mediarestrict.enforce_media_restriction(message)

    joined_at.assert_not_awaited()
    bot.delete_message.assert_not_awaited()
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_enforce_never_restricts_an_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        mediarestrict, "context_for", AsyncMock(return_value=_FakeCtx(is_admin=True))
    )
    joined_at = AsyncMock()
    monkeypatch.setattr(mediarestrict, "_joined_at", joined_at)

    bot = type("Bot", (), {"delete_message": AsyncMock(), "send_message": AsyncMock()})()
    message = _FakeMessage(from_user=_FakeUser(id=1), bot=bot)
    with pytest.raises(SkipHandler):
        await mediarestrict.enforce_media_restriction(message)

    joined_at.assert_not_awaited()
    bot.delete_message.assert_not_awaited()
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_enforce_fails_open_when_join_was_never_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No `group_members` row -- e.g. the router-ordering race documented in
    the module docstring. Must not restrict an unknown-join-time member."""
    monkeypatch.setattr(mediarestrict, "context_for", AsyncMock(return_value=_FakeCtx()))
    monkeypatch.setattr(mediarestrict, "_joined_at", AsyncMock(return_value=None))

    bot = type("Bot", (), {"delete_message": AsyncMock(), "send_message": AsyncMock()})()
    message = _FakeMessage(from_user=_FakeUser(id=1), bot=bot)
    with pytest.raises(SkipHandler):
        await mediarestrict.enforce_media_restriction(message)

    bot.delete_message.assert_not_awaited()
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_enforce_allows_a_long_time_member(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mediarestrict, "context_for", AsyncMock(return_value=_FakeCtx()))
    monkeypatch.setattr(
        mediarestrict,
        "_joined_at",
        AsyncMock(return_value=datetime.now(UTC) - timedelta(hours=1)),
    )

    bot = type("Bot", (), {"delete_message": AsyncMock(), "send_message": AsyncMock()})()
    message = _FakeMessage(from_user=_FakeUser(id=1), bot=bot)
    with pytest.raises(SkipHandler):
        await mediarestrict.enforce_media_restriction(message)

    bot.delete_message.assert_not_awaited()
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_enforce_deletes_and_warns_a_brand_new_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        mediarestrict,
        "context_for",
        AsyncMock(return_value=_FakeCtx(group_id=-100, media_restrict_seconds=600)),
    )
    monkeypatch.setattr(mediarestrict, "_joined_at", AsyncMock(return_value=datetime.now(UTC)))

    bot = type("Bot", (), {"delete_message": AsyncMock(), "send_message": AsyncMock()})()
    message = _FakeMessage(from_user=_FakeUser(id=1), bot=bot, message_id=77)
    await mediarestrict.enforce_media_restriction(message)

    bot.delete_message.assert_awaited_once_with(-100, 77)
    bot.send_message.assert_awaited_once()
    (chat_id, text), _kwargs = bot.send_message.call_args
    assert chat_id == -100
    assert "10" in text  # round(600/60) minutes, ported verbatim in restrict_message


@pytest.mark.asyncio
async def test_enforce_still_warns_even_if_delete_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deletion needs `can_delete_messages`, which v1 never needed for this
    feature at all -- a missing permission must not swallow the warning too."""
    monkeypatch.setattr(mediarestrict, "context_for", AsyncMock(return_value=_FakeCtx()))
    monkeypatch.setattr(mediarestrict, "_joined_at", AsyncMock(return_value=datetime.now(UTC)))

    bot = type(
        "Bot",
        (),
        {
            "delete_message": AsyncMock(side_effect=RuntimeError("no rights")),
            "send_message": AsyncMock(),
        },
    )()
    message = _FakeMessage(from_user=_FakeUser(id=1), bot=bot)
    await mediarestrict.enforce_media_restriction(message)

    bot.send_message.assert_awaited_once()


class TestJoinChainResilience:
    """`record_join` runs first in the join chain (handlers/__init__.py).

    Anything it raises replaces the doomlist ban, the captcha and the welcome
    with silence, so a database outage must not escape it.
    """

    async def test_a_database_outage_still_lets_the_rest_of_the_join_chain_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from aiogram.dispatcher.event.bases import SkipHandler

        from cb_gateway.handlers import mediarestrict as mod

        async def boom(*args: object, **kwargs: object) -> None:
            raise RuntimeError("pg pool not initialised")

        monkeypatch.setattr(mod, "_record_join", boom)

        class _User:
            id = 4242
            is_bot = False

        class _Bot:
            id = 424242

        class _Chat:
            id = -100999

        class _Message:
            new_chat_members: ClassVar[list] = [_User()]
            bot = _Bot()
            chat = _Chat()

        # SkipHandler, not RuntimeError: the next router still gets the join.
        with pytest.raises(SkipHandler):
            await mod.record_join(_Message())
