"""`cb_worker.jobs.broadcast` — the fan-out v1 ran inline with a sleep loop.

v1: `broadcast_message`, `Miscellaneous.py:114-122` — `for group: send;
sleep(0.5)` on a handler thread, wrapped in `except: pass`, reporting nothing.
"""

from __future__ import annotations

from typing import Any

import pytest

from cb_core import jobs
from cb_worker.jobs import broadcast


class _Redis:
    def __init__(self) -> None:
        self.jobs: list[dict[str, Any]] = []

    async def enqueue_job(self, name: str, **kwargs: Any) -> None:
        self.jobs.append({"name": name, **kwargs})


class _Bot:
    def __init__(self, *, fail_for: set[int] | None = None) -> None:
        self.fail_for = fail_for or set()
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> None:
        if chat_id in self.fail_for:
            raise RuntimeError("bot was removed from the chat")
        self.sent.append((chat_id, text))


@pytest.fixture
def groups(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    ids = [-100, -200, -300]

    async def _all_group_ids() -> tuple[int, ...]:
        return tuple(ids)

    monkeypatch.setattr(broadcast.ops, "all_group_ids", _all_group_ids)
    return ids


async def test_one_deferred_job_per_group(groups: list[int]) -> None:
    redis, bot = _Redis(), _Bot()
    queued = await broadcast.broadcast_to_groups(
        {"redis": redis, "bot": bot}, text="hello", owner_id=7
    )
    assert queued == 3
    assert [job["name"] for job in redis.jobs] == [jobs.BROADCAST_DELIVER] * 3
    assert [job["group_id"] for job in redis.jobs] == groups


async def test_the_sends_are_spaced_rather_than_slept_between(groups: list[int]) -> None:
    """v1 blocked a thread for 0.5s per group (FEATURE-MAP D8)."""
    redis, bot = _Redis(), _Bot()
    await broadcast.broadcast_to_groups({"redis": redis, "bot": bot}, text="hi", owner_id=7)
    assert [job["_defer_by"] for job in redis.jobs] == [
        0 * broadcast.SPACING_SECONDS,
        1 * broadcast.SPACING_SECONDS,
        2 * broadcast.SPACING_SECONDS,
    ]


async def test_the_owner_is_told_how_many_groups(groups: list[int]) -> None:
    """v1 reported nothing at all, so a fan-out that reached nobody looked
    identical to one that reached everybody."""
    redis, bot = _Redis(), _Bot()
    await broadcast.broadcast_to_groups({"redis": redis, "bot": bot}, text="hi", owner_id=7)
    assert bot.sent == [(7, "Broadcasting to 3 of 3 groups.")]


async def test_delivery_sends_the_text_to_the_group() -> None:
    bot = _Bot()
    await broadcast.deliver_broadcast({"bot": bot}, group_id=-100, text="hello everyone")
    assert bot.sent == [(-100, "hello everyone")]


async def test_a_chat_the_bot_cannot_post_to_does_not_raise() -> None:
    """v1's `except: pass`, but counted rather than invisible."""
    bot = _Bot(fail_for={-100})
    await broadcast.deliver_broadcast({"bot": bot}, group_id=-100, text="hello")
    assert bot.sent == []
