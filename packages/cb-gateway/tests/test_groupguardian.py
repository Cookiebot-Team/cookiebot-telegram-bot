"""Unit coverage for core_groupguardian — pure logic and mocked DB seam only, no
dispatcher, no real Telegram, no real database.

See docs/contracts/core_groupguardian.md for the full behaviour contract and
qa/features/core_groupguardian.feature + qa/test_core_groupguardian.py for the
end-to-end version of the same assertions. qa/integration/test_captcha_challenges.py
covers the real `captcha_challenges` round trip.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiogram.dispatcher.event.bases import SkipHandler

from cb_core.group_config import GroupConfig
from cb_gateway.handlers import groupguardian as gg


@dataclass
class _FakeUser:
    id: int
    username: str | None = None
    first_name: str | None = "Newcomer"
    is_bot: bool = False


@dataclass
class _FakeChat:
    id: int = -100
    title: str | None = "QA Group"


@dataclass
class _FakeMessage:
    """Only the attributes the handler / filters actually read."""

    text: str | None = None
    from_user: _FakeUser | None = None
    new_chat_members: Any = None
    chat: _FakeChat = field(default_factory=_FakeChat)
    bot: Any = None
    message_id: int = 1

    async def reply(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError

    async def delete(self) -> None:  # pragma: no cover
        raise NotImplementedError


@dataclass
class _FakeCallbackMessage:
    chat: _FakeChat = field(default_factory=_FakeChat)
    message_id: int = 500


@dataclass
class _FakeCallback:
    data: str
    from_user: _FakeUser | None = None
    message: Any = None
    bot: Any = None

    async def answer(self, *args: Any, **kwargs: Any) -> None:
        return None


class _FakeCtx:
    def __init__(
        self,
        *,
        group_id: int = -100,
        is_admin: bool = False,
        lang: str = "en",
        captcha_timeout_seconds: int = 300,
    ) -> None:
        self.group_id = group_id
        self.is_admin = is_admin
        self.lang = lang
        self.config = GroupConfig(
            group_id=group_id, captcha_timeout_seconds=captcha_timeout_seconds
        )


def _bot(**extra: Any) -> Any:
    return type("Bot", (), {"id": 424242, **extra})()


# ------------------------------------------------------------------ join gating


@pytest.mark.asyncio
async def test_on_join_skips_when_no_joiners(monkeypatch: pytest.MonkeyPatch) -> None:
    context_for = AsyncMock()
    monkeypatch.setattr(gg, "context_for", context_for)

    await gg.on_join(_FakeMessage(new_chat_members=[]))

    context_for.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_join_skips_the_bots_own_join(monkeypatch: pytest.MonkeyPatch) -> None:
    context_for = AsyncMock()
    monkeypatch.setattr(gg, "context_for", context_for)

    bot = _bot()
    message = _FakeMessage(new_chat_members=[_FakeUser(id=424242)], bot=bot)

    with pytest.raises(SkipHandler):
        await gg.on_join(message)
    context_for.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_join_skips_an_invited_member() -> None:
    """v1: `msg['from']['id'] != msg['new_chat_participant']['id']` -> always
    `welcome_message`, captcha never considered (COOKIEBOT.py:136-141)."""
    bot = _bot()
    newcomer = _FakeUser(id=2)
    inviter = _FakeUser(id=3)
    message = _FakeMessage(new_chat_members=[newcomer], from_user=inviter, bot=bot)

    with pytest.raises(SkipHandler):
        await gg.on_join(message)


@pytest.mark.asyncio
async def test_on_join_skips_when_captcha_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    newcomer = _FakeUser(id=2)
    message = _FakeMessage(new_chat_members=[newcomer], from_user=newcomer, bot=_bot())
    monkeypatch.setattr(
        gg,
        "context_for",
        AsyncMock(return_value=_FakeCtx(captcha_timeout_seconds=0)),
    )
    monkeypatch.setattr(gg.admins, "is_admin", AsyncMock(return_value=True))

    with pytest.raises(SkipHandler):
        await gg.on_join(message)


@pytest.mark.asyncio
async def test_on_join_skips_when_bot_is_not_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    newcomer = _FakeUser(id=2)
    message = _FakeMessage(new_chat_members=[newcomer], from_user=newcomer, bot=_bot())
    monkeypatch.setattr(gg, "context_for", AsyncMock(return_value=_FakeCtx()))
    monkeypatch.setattr(gg.admins, "is_admin", AsyncMock(return_value=False))

    with pytest.raises(SkipHandler):
        await gg.on_join(message)


@pytest.mark.asyncio
async def test_on_join_issues_a_challenge_when_gate_is_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    newcomer = _FakeUser(id=2, first_name="Newbie")
    sent = type("Sent", (), {"message_id": 777})()
    message = _FakeMessage(new_chat_members=[newcomer], from_user=newcomer, bot=_bot())
    message.reply = AsyncMock(return_value=sent)

    monkeypatch.setattr(gg, "context_for", AsyncMock(return_value=_FakeCtx()))
    monkeypatch.setattr(gg.admins, "is_admin", AsyncMock(return_value=True))
    issue_row = AsyncMock()
    monkeypatch.setattr(gg, "_issue_row", issue_row)

    await gg.on_join(message)

    message.reply.assert_awaited_once()
    call = message.reply.await_args
    assert "keyboard" not in call.kwargs or call.kwargs.get("reply_markup") is not None
    issue_row.assert_awaited_once()
    args = issue_row.await_args.args
    assert args[0] == -100
    assert args[1] == 2
    assert args[3] == 777


# --------------------------------------------------------------- reply filter


@pytest.mark.asyncio
async def test_is_captcha_reply_false_with_no_text() -> None:
    assert await gg._is_captcha_reply(_FakeMessage(text=None, from_user=_FakeUser(id=1))) is False  # noqa: SLF001


@pytest.mark.asyncio
async def test_is_captcha_reply_false_for_a_command() -> None:
    message = _FakeMessage(text="/rules", from_user=_FakeUser(id=1))
    assert await gg._is_captcha_reply(message) is False  # noqa: SLF001


@pytest.mark.asyncio
async def test_is_captcha_reply_true_for_bare_slash(monkeypatch: pytest.MonkeyPatch) -> None:
    """v1's guard is `startswith('/') and len(text) > 1` — a lone "/" still
    counts as a solve attempt, same quirk `welcome.py`/`rules.py` preserve."""
    message = _FakeMessage(text="/", from_user=_FakeUser(id=1))
    monkeypatch.setattr(
        gg.group_config,
        "get_config",
        AsyncMock(return_value=GroupConfig(group_id=-100, captcha_timeout_seconds=300)),
    )
    monkeypatch.setattr(
        gg, "_fetch_pending", AsyncMock(return_value={"answer": "7", "attempts": 0})
    )

    result = await gg._is_captcha_reply(message)  # noqa: SLF001

    assert result == {"captcha_row": {"answer": "7", "attempts": 0}}


@pytest.mark.asyncio
async def test_is_captcha_reply_false_when_gate_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        gg.group_config,
        "get_config",
        AsyncMock(return_value=GroupConfig(group_id=-100, captcha_timeout_seconds=0)),
    )
    fetch = AsyncMock()
    monkeypatch.setattr(gg, "_fetch_pending", fetch)

    message = _FakeMessage(text="hello", from_user=_FakeUser(id=1))
    result = await gg._is_captcha_reply(message)  # noqa: SLF001

    assert result is False
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_is_captcha_reply_false_when_no_pending_row(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        gg.group_config,
        "get_config",
        AsyncMock(return_value=GroupConfig(group_id=-100, captcha_timeout_seconds=300)),
    )
    monkeypatch.setattr(gg, "_fetch_pending", AsyncMock(return_value=None))

    message = _FakeMessage(text="hello", from_user=_FakeUser(id=1))
    assert await gg._is_captcha_reply(message) is False  # noqa: SLF001


# --------------------------------------------------------------- verify + kick


@pytest.mark.asyncio
async def test_resolve_attempt_correct_answer_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    succeed = AsyncMock()
    fail = AsyncMock()
    monkeypatch.setattr(gg, "_succeed", succeed)
    monkeypatch.setattr(gg, "_fail_attempt", fail)

    ctx = _FakeCtx()
    row = {"answer": "13", "attempts": 0}
    sender = _FakeUser(id=2)

    await gg._resolve_attempt(ctx, _bot(), -100, "QA Group", sender, "13", row)  # noqa: SLF001

    succeed.assert_awaited_once()
    fail.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_attempt_wrong_answer_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    succeed = AsyncMock()
    fail = AsyncMock()
    monkeypatch.setattr(gg, "_succeed", succeed)
    monkeypatch.setattr(gg, "_fail_attempt", fail)

    ctx = _FakeCtx()
    row = {"answer": "13", "attempts": 0}
    sender = _FakeUser(id=2)

    await gg._resolve_attempt(ctx, _bot(), -100, "QA Group", sender, "99", row)  # noqa: SLF001

    fail.assert_awaited_once()
    succeed.assert_not_awaited()


@pytest.mark.asyncio
async def test_fail_attempt_below_max_records_wrong_attempt_and_replies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = AsyncMock()
    kick = AsyncMock()
    monkeypatch.setattr(gg, "_record_wrong_attempt", record)
    monkeypatch.setattr(gg, "_kick", kick)

    bot = _bot(send_message=AsyncMock())
    ctx = _FakeCtx()
    future_expiry = datetime.now(UTC) + timedelta(minutes=5)
    row = {"answer": "13", "attempts": 0, "expires_at": future_expiry}

    await gg._fail_attempt(ctx, bot, -100, 2, row)  # noqa: SLF001

    record.assert_awaited_once_with(-100, 2, 1)
    kick.assert_not_awaited()
    bot.send_message.assert_awaited_once_with(-100, gg.WRONG_ANSWER_TEXT)


@pytest.mark.asyncio
async def test_fail_attempt_hitting_max_attempts_kicks_with_limit_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v1: `check_captcha` picks `reason = captcha.limit` when `attempts <= 0`,
    checked before the time condition (GroupShield.py:288-292)."""
    kick = AsyncMock()
    monkeypatch.setattr(gg, "_kick", kick)

    ctx = _FakeCtx()
    future_expiry = datetime.now(UTC) + timedelta(minutes=5)
    row = {"answer": "13", "attempts": gg.MAX_ATTEMPTS - 1, "expires_at": future_expiry}

    await gg._fail_attempt(ctx, _bot(), -100, 2, row)  # noqa: SLF001

    kick.assert_awaited_once()
    assert kick.await_args.args[0] is ctx
    assert kick.await_args.args[2:] == (-100, 2)
    assert kick.await_args.kwargs == {"reason_key": "limit"}


@pytest.mark.asyncio
async def test_fail_attempt_past_expiry_kicks_with_time_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kick = AsyncMock()
    monkeypatch.setattr(gg, "_kick", kick)

    ctx = _FakeCtx()
    past_expiry = datetime.now(UTC) - timedelta(seconds=1)
    row = {"answer": "13", "attempts": 0, "expires_at": past_expiry}

    await gg._fail_attempt(ctx, _bot(), -100, 2, row)  # noqa: SLF001

    assert kick.await_args.kwargs == {"reason_key": "time"}


@pytest.mark.asyncio
async def test_kick_bans_sends_kick_text_and_schedules_unban(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ban = AsyncMock()
    send = AsyncMock()
    unban = AsyncMock()
    bot = _bot(ban_chat_member=ban, send_message=send, unban_chat_member=unban)
    delete = AsyncMock()
    monkeypatch.setattr(gg, "_delete_challenge", delete)

    # Avoid a real 30s sleep in the test process.
    async def _fake_delayed_unban(bot_arg: Any, chat_id: int, user_id: int) -> None:
        await bot_arg.unban_chat_member(chat_id, user_id)

    monkeypatch.setattr(gg, "_delayed_unban", _fake_delayed_unban)

    ctx = _FakeCtx(lang="en")
    await gg._kick(ctx, bot, -100, 2, reason_key="limit")  # noqa: SLF001

    ban.assert_awaited_once_with(-100, 2)
    delete.assert_awaited_once_with(-100, 2)
    send.assert_awaited_once()
    body = send.await_args.args[1]
    assert "2" in body


@pytest.mark.asyncio
async def test_kick_failure_sends_error_kick_text_and_does_not_schedule_unban(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aiogram.exceptions import TelegramBadRequest

    ban = AsyncMock(side_effect=TelegramBadRequest(method=None, message="no rights"))
    send = AsyncMock()
    bot = _bot(ban_chat_member=ban, send_message=send)
    monkeypatch.setattr(gg, "_delete_challenge", AsyncMock())
    delayed = AsyncMock()
    monkeypatch.setattr(gg, "_delayed_unban", delayed)

    ctx = _FakeCtx(lang="en")
    await gg._kick(ctx, bot, -100, 2, reason_key="time")  # noqa: SLF001

    send.assert_awaited_once()
    delayed.assert_not_called()


# ---------------------------------------------------------------- button path


@pytest.mark.asyncio
async def test_callback_ignores_payloads_that_are_not_ours() -> None:
    callback = _FakeCallback(data="cap:", from_user=_FakeUser(id=1))
    # parse_callback returns ("", "") for a malformed "cap:" payload with no
    # option separator — the handler must not raise.
    await gg.on_captcha_callback(callback)


@pytest.mark.asyncio
async def test_callback_newcomer_taps_the_correct_option_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = {"user_id": 2, "nonce": "abc", "answer": "13", "attempts": 0}
    monkeypatch.setattr(gg, "_fetch_by_message", AsyncMock(return_value=row))
    resolve = AsyncMock()
    monkeypatch.setattr(gg, "_resolve_attempt", resolve)
    monkeypatch.setattr(gg, "context_for", AsyncMock(return_value=_FakeCtx()))

    callback = _FakeCallback(
        data="cap:abc:13", from_user=_FakeUser(id=2), message=_FakeCallbackMessage(), bot=_bot()
    )
    await gg.on_captcha_callback(callback)

    resolve.assert_awaited_once()
    assert resolve.await_args.args[5] == "13"


@pytest.mark.asyncio
async def test_callback_newcomer_tapping_approve_button_has_no_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The self-tap free pass is the fixed v1 defect — a newcomer tapping the
    admin-only approve button must not succeed."""
    row = {"user_id": 2, "nonce": "abc", "answer": "13", "attempts": 0}
    monkeypatch.setattr(gg, "_fetch_by_message", AsyncMock(return_value=row))
    resolve = AsyncMock()
    succeed = AsyncMock()
    monkeypatch.setattr(gg, "_resolve_attempt", resolve)
    monkeypatch.setattr(gg, "_succeed", succeed)
    monkeypatch.setattr(gg, "context_for", AsyncMock(return_value=_FakeCtx(is_admin=False)))

    callback = _FakeCallback(
        data=f"cap:abc:{gg._APPROVE_OPTION}",  # noqa: SLF001
        from_user=_FakeUser(id=2),
        message=_FakeCallbackMessage(),
        bot=_bot(),
    )
    await gg.on_captcha_callback(callback)

    resolve.assert_not_awaited()
    succeed.assert_not_awaited()


@pytest.mark.asyncio
async def test_callback_admin_approve_bypasses_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = {"user_id": 2, "nonce": "abc", "answer": "13", "attempts": 0}
    monkeypatch.setattr(gg, "_fetch_by_message", AsyncMock(return_value=row))
    succeed = AsyncMock()
    monkeypatch.setattr(gg, "_succeed", succeed)
    monkeypatch.setattr(gg, "context_for", AsyncMock(return_value=_FakeCtx(is_admin=True)))

    member = type("Member", (), {"user": _FakeUser(id=2)})()
    bot = _bot(get_chat_member=AsyncMock(return_value=member))
    callback = _FakeCallback(
        data=f"cap:abc:{gg._APPROVE_OPTION}",  # noqa: SLF001
        from_user=_FakeUser(id=999),
        message=_FakeCallbackMessage(),
        bot=bot,
    )
    await gg.on_captcha_callback(callback)

    succeed.assert_awaited_once()


@pytest.mark.asyncio
async def test_callback_non_admin_stranger_has_no_effect(monkeypatch: pytest.MonkeyPatch) -> None:
    row = {"user_id": 2, "nonce": "abc", "answer": "13", "attempts": 0}
    monkeypatch.setattr(gg, "_fetch_by_message", AsyncMock(return_value=row))
    succeed = AsyncMock()
    resolve = AsyncMock()
    monkeypatch.setattr(gg, "_succeed", succeed)
    monkeypatch.setattr(gg, "_resolve_attempt", resolve)
    monkeypatch.setattr(gg, "context_for", AsyncMock(return_value=_FakeCtx(is_admin=False)))

    callback = _FakeCallback(
        data="cap:abc:13", from_user=_FakeUser(id=999), message=_FakeCallbackMessage(), bot=_bot()
    )
    await gg.on_captcha_callback(callback)

    succeed.assert_not_awaited()
    resolve.assert_not_awaited()


@pytest.mark.asyncio
async def test_callback_stale_nonce_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    row = {"user_id": 2, "nonce": "current", "answer": "13", "attempts": 0}
    monkeypatch.setattr(gg, "_fetch_by_message", AsyncMock(return_value=row))
    resolve = AsyncMock()
    monkeypatch.setattr(gg, "_resolve_attempt", resolve)
    monkeypatch.setattr(gg, "context_for", AsyncMock(return_value=_FakeCtx()))

    callback = _FakeCallback(
        data="cap:stale:13", from_user=_FakeUser(id=2), message=_FakeCallbackMessage(), bot=_bot()
    )
    await gg.on_captcha_callback(callback)

    resolve.assert_not_awaited()


# -------------------------------------------------------------------- strings


class TestCaptchaStrings:
    def test_returns_nested_captcha_object(self) -> None:
        strings = gg._captcha_strings("en")  # noqa: SLF001
        assert "title" in strings
        assert "%(name)s" in strings["title"]

    def test_falls_back_to_en_for_unknown_language(self) -> None:
        strings = gg._captcha_strings("xx")  # noqa: SLF001
        assert strings == gg._captcha_strings("en")  # noqa: SLF001
