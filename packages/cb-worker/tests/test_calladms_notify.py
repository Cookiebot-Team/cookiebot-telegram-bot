"""Unit coverage for `cb_worker.jobs.calladms` (design R3, R4).

Pure logic and mocked seams only — `cb_core.admins.admin_ids` and a fake
`aiogram.Bot`, no real Telegram, no real database. The gateway half
(confirmation prompt, staleness window, group ping, enqueue) is
`packages/cb-gateway/tests/test_calladms.py`; this file only covers what
happens per admin once the job runs. Model:
`packages/cb-worker/tests/test_everyone_fanout.py`.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from cb_worker.jobs import calladms as job


def _bot(bot_id: int, **overrides: Any) -> AsyncMock:
    bot = AsyncMock()
    bot.id = bot_id
    for name, value in overrides.items():
        setattr(bot, name, value)
    return bot


class TestDeepLink:
    def test_strips_the_supergroup_prefix(self) -> None:
        # v1: `str(chat['id']).replace('-100', '')` (UserRegisters.py:199).
        assert job._deep_link(-1001234567890, 42) == "https://t.me/c/1234567890/42"  # noqa: SLF001

    def test_bare_id_without_the_prefix_is_unchanged(self) -> None:
        assert job._deep_link(1234567890, 42) == "https://t.me/c/1234567890/42"  # noqa: SLF001


class TestNotifyButton:
    """v1's exact substring test (`:199`): a button only when `-100` occurs
    anywhere in the chat id's string form, no `reply_markup` at all otherwise
    — not aiogram's own "starts with -100" supergroup convention."""

    async def test_supergroup_id_gets_a_show_message_button(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(job.admins, "admin_ids", AsyncMock(return_value=frozenset({1})))
        monkeypatch.setattr(job.asyncio, "sleep", AsyncMock())
        bot = _bot(999, send_message=AsyncMock())

        await job._notify(bot, -1001234567890, "QA Group", 42, "en")  # noqa: SLF001

        kwargs = bot.send_message.await_args.kwargs
        assert kwargs["reply_markup"] is not None
        button = kwargs["reply_markup"].inline_keyboard[0][0]
        assert button.text == "Show message"
        assert button.url == "https://t.me/c/1234567890/42"

    async def test_non_supergroup_id_gets_no_button(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(job.admins, "admin_ids", AsyncMock(return_value=frozenset({1})))
        monkeypatch.setattr(job.asyncio, "sleep", AsyncMock())
        bot = _bot(999, send_message=AsyncMock())

        await job._notify(bot, -123456789, "QA Group", 42, "en")  # noqa: SLF001

        assert bot.send_message.await_args.kwargs["reply_markup"] is None


class TestNotifyExcludesTheBot:
    async def test_the_bots_own_id_is_never_dmed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # v1: `int(user[0]['id']) == int(myself['id'])` (`:192`).
        monkeypatch.setattr(job.admins, "admin_ids", AsyncMock(return_value=frozenset({1, 2, 999})))
        monkeypatch.setattr(job.asyncio, "sleep", AsyncMock())
        bot = _bot(999, send_message=AsyncMock())

        sent = await job._notify(bot, 555, "QA Group", 42, "en")  # noqa: SLF001

        assert sent == 2
        dmed_ids = {call.args[0] for call in bot.send_message.await_args_list}
        assert dmed_ids == {1, 2}


class TestNotifyEmptyAdmins:
    async def test_no_admins_resolved_sends_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A Telegram outage degrades `cb_core.admins.admin_ids` to an empty
        set rather than raising (`docs/contracts/admins.md`); the job must
        not crash on that, just DM nobody."""
        monkeypatch.setattr(job.admins, "admin_ids", AsyncMock(return_value=frozenset()))
        monkeypatch.setattr(job.asyncio, "sleep", AsyncMock())
        bot = _bot(999, send_message=AsyncMock())

        sent = await job._notify(bot, 555, "QA Group", 42, "en")  # noqa: SLF001

        assert sent == 0
        bot.send_message.assert_not_awaited()


class TestNotifySendFailureDoesNotAbort:
    async def test_a_raising_send_does_not_abort_the_loop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(job.admins, "admin_ids", AsyncMock(return_value=frozenset({10, 11})))
        monkeypatch.setattr(job.asyncio, "sleep", AsyncMock())
        bot = _bot(
            999,
            send_message=AsyncMock(
                side_effect=[Exception("Forbidden: bot was blocked by the user"), None]
            ),
        )

        blocked_before = job.calladms_dm_total.labels(outcome="blocked")._value.get()  # noqa: SLF001
        sent_before = job.calladms_dm_total.labels(outcome="sent")._value.get()  # noqa: SLF001

        sent = await job._notify(bot, 555, "QA Group", 42, "en")  # noqa: SLF001

        # Both admins were attempted despite the first raising.
        assert bot.send_message.await_count == 2
        assert sent == 1
        assert job.calladms_dm_total.labels(outcome="blocked")._value.get() == blocked_before + 1  # noqa: SLF001
        assert job.calladms_dm_total.labels(outcome="sent")._value.get() == sent_before + 1  # noqa: SLF001


class TestNotifyAdminsOfCallWrapper:
    async def test_full_job_runs_without_raising_and_uses_the_context_bot(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`notify_admins_of_call` is what's registered in
        `WorkerSettings.functions` — the arq-facing wrapper around `_notify`,
        sourcing the bot from `ctx["bot"]` (design R1.2)."""
        monkeypatch.setattr(job.admins, "admin_ids", AsyncMock(return_value=frozenset({1})))
        monkeypatch.setattr(job.asyncio, "sleep", AsyncMock())
        bot = _bot(999, send_message=AsyncMock())
        ctx: dict[str, Any] = {"bot": bot}

        await job.notify_admins_of_call(
            ctx,
            group_id=555,
            chat_title="QA Group",
            original_message_id=42,
            lang="en",
        )

        bot.send_message.assert_awaited_once()
