"""Unit coverage for `cb_worker.jobs.everyone` (design R5, R6.2).

Pure logic and mocked seams only — `members.roster`/`members.mark_left` and a
fake `aiogram.Bot`, no real Telegram, no real database. The gateway half
(admin gate, chunked ping, enqueue) is `packages/cb-gateway/tests/test_everyone.py`
(T4); this file only covers what happens per member once the job runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiogram.enums import ChatMemberStatus

from cb_core.members import MemberRef
from cb_worker.jobs import everyone as job


@dataclass
class _FakeChatMember:
    status: str


def _bot(**overrides: Any) -> AsyncMock:
    bot = AsyncMock()
    for name, value in overrides.items():
        setattr(bot, name, value)
    return bot


class TestDeepLink:
    def test_strips_the_supergroup_prefix(self) -> None:
        # v1: `str(chat['id']).replace('-100', '')` (UserRegisters.py:142).
        assert job._deep_link(-1001234567890, 42) == "https://t.me/c/1234567890/42"  # noqa: SLF001

    def test_bare_id_without_the_prefix_is_unchanged(self) -> None:
        assert job._deep_link(1234567890, 42) == "https://t.me/c/1234567890/42"  # noqa: SLF001


class TestFanoutLeftKicked:
    async def test_left_or_kicked_marks_left_and_skips_the_dm(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        roster = (
            MemberRef(user_id=1, username="left_member"),
            MemberRef(user_id=2, username="kicked_member"),
            MemberRef(user_id=3, username="active_member"),
        )
        monkeypatch.setattr(job.members, "roster", AsyncMock(return_value=roster))
        mark_left = AsyncMock()
        monkeypatch.setattr(job.members, "mark_left", mark_left)
        monkeypatch.setattr(job.asyncio, "sleep", AsyncMock())

        statuses = {
            1: _FakeChatMember(status=ChatMemberStatus.LEFT),
            2: _FakeChatMember(status=ChatMemberStatus.KICKED),
            3: _FakeChatMember(status=ChatMemberStatus.MEMBER),
        }
        bot = _bot(
            get_chat_member=AsyncMock(side_effect=lambda chat_id, uid: statuses[uid]),
            send_message=AsyncMock(),
        )

        left_before = job.everyone_dm_total.labels(outcome="left")._value.get()  # noqa: SLF001
        sent_before = job.everyone_dm_total.labels(outcome="sent")._value.get()  # noqa: SLF001

        sent = await job._fanout(bot, 555, -100999, 7, "QA Group", "en")  # noqa: SLF001

        assert sent == 1
        assert mark_left.await_args_list == [
            ((555, 1), {}),
            ((555, 2), {}),
        ]
        # Only the still-active member gets DM'd.
        assert bot.send_message.await_count == 1
        assert bot.send_message.await_args.args[0] == 3
        assert job.everyone_dm_total.labels(outcome="left")._value.get() == left_before + 2  # noqa: SLF001
        assert job.everyone_dm_total.labels(outcome="sent")._value.get() == sent_before + 1  # noqa: SLF001


class TestFanoutSendFailureDoesNotAbort:
    async def test_a_raising_send_does_not_abort_the_loop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        roster = (
            MemberRef(user_id=10, username="blocked_the_bot"),
            MemberRef(user_id=11, username="still_reachable"),
        )
        monkeypatch.setattr(job.members, "roster", AsyncMock(return_value=roster))
        monkeypatch.setattr(job.members, "mark_left", AsyncMock())
        monkeypatch.setattr(job.asyncio, "sleep", AsyncMock())

        member_status = _FakeChatMember(status=ChatMemberStatus.MEMBER)
        bot = _bot(
            get_chat_member=AsyncMock(return_value=member_status),
            send_message=AsyncMock(
                side_effect=[Exception("Forbidden: bot was blocked by the user"), None]
            ),
        )

        blocked_before = job.everyone_dm_total.labels(outcome="blocked")._value.get()  # noqa: SLF001
        sent_before = job.everyone_dm_total.labels(outcome="sent")._value.get()  # noqa: SLF001

        sent = await job._fanout(bot, 555, -100999, 7, "QA Group", "en")  # noqa: SLF001

        # Both members were attempted despite the first raising.
        assert bot.send_message.await_count == 2
        assert sent == 1
        assert job.everyone_dm_total.labels(outcome="blocked")._value.get() == blocked_before + 1  # noqa: SLF001
        assert job.everyone_dm_total.labels(outcome="sent")._value.get() == sent_before + 1  # noqa: SLF001

    async def test_a_raising_get_chat_member_does_not_abort_the_loop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        roster = (
            MemberRef(user_id=20, username="unresolvable"),
            MemberRef(user_id=21, username="resolvable"),
        )
        monkeypatch.setattr(job.members, "roster", AsyncMock(return_value=roster))
        monkeypatch.setattr(job.members, "mark_left", AsyncMock())
        monkeypatch.setattr(job.asyncio, "sleep", AsyncMock())

        member_status = _FakeChatMember(status=ChatMemberStatus.MEMBER)
        bot = _bot(
            get_chat_member=AsyncMock(
                side_effect=[Exception("Bad Request: user not found"), member_status]
            ),
            send_message=AsyncMock(),
        )

        error_before = job.everyone_dm_total.labels(outcome="error")._value.get()  # noqa: SLF001

        sent = await job._fanout(bot, 555, -100999, 7, "QA Group", "en")  # noqa: SLF001

        assert sent == 1
        assert bot.send_message.await_count == 1
        assert job.everyone_dm_total.labels(outcome="error")._value.get() == error_before + 1  # noqa: SLF001


class TestEveryoneFanoutWrapper:
    async def test_full_job_runs_without_raising_and_uses_the_context_bot(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`everyone_fanout` is what's registered in `WorkerSettings.functions`
        (design R5.1) — the arq-facing wrapper around `_fanout`, sourcing the
        bot from `ctx["bot"]` (design R3.1)."""
        roster = (MemberRef(user_id=1, username="only_member"),)
        monkeypatch.setattr(job.members, "roster", AsyncMock(return_value=roster))
        monkeypatch.setattr(job.asyncio, "sleep", AsyncMock())

        bot = _bot(
            get_chat_member=AsyncMock(return_value=_FakeChatMember(status=ChatMemberStatus.MEMBER)),
            send_message=AsyncMock(),
        )
        ctx: dict[str, Any] = {"bot": bot}

        await job.everyone_fanout(
            ctx,
            group_id=555,
            chat_id=-100999,
            message_id=7,
            chat_title="QA Group",
            lang="en",
        )

        bot.send_message.assert_awaited_once()
