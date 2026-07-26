"""LLM accounting against the database.

v1 called OpenAI from three places with no accounting, so "which group is
spending the money" had no answer. These tests assert the row exists, is scoped
to the group, and rolls up correctly.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator, Callable, Coroutine, Sequence
from types import ModuleType
from typing import TYPE_CHECKING, Any

import pytest

from cb_core.llm import LLMRouter, Message, TaskConfig, Usage
from cb_core.llm.types import Completion, Transcript

if TYPE_CHECKING:
    from qa.integration.factories import World

pytestmark = pytest.mark.integration


class _StubProvider:
    def __init__(self, completion: Completion) -> None:
        self.completion = completion

    @property
    def name(self) -> str:
        return "stub"

    async def complete(self, messages: Sequence[Message], **kwargs: object) -> Completion:
        return self.completion

    async def stream(  # pragma: no cover
        self, messages: Sequence[Message], **kwargs: object
    ) -> AsyncIterator[str]:
        yield ""

    async def count_tokens(
        self, messages: Sequence[Message], *, model: str, system: str | None = None
    ) -> int:
        return 1

    async def transcribe(
        self,
        audio: bytes,
        *,
        model: str,
        filename: str = "a.ogg",
        language: str | None = None,
    ) -> Transcript:
        raise NotImplementedError

    async def close(self) -> None:
        return None


def _router(completion: Completion) -> LLMRouter:
    return LLMRouter(
        {"stub": _StubProvider(completion)},
        {"chat": TaskConfig(provider="stub", model="stub-model", max_tokens=64)},
    )


def _completion(**kw: object) -> Completion:
    base: dict[str, object] = {
        "text": "hello",
        "model": "stub-model",
        "provider": "stub",
        "usage": Usage(input_tokens=120, output_tokens=45, cache_read_tokens=80),
        "cost_usd": 0.0031,
    }
    base.update(kw)
    return Completion(**base)


class TestUsageRows:
    def test_call_writes_one_row(
        self, run: Callable[[Coroutine[Any, Any, Any]], Any], world: World
    ) -> None:
        user = world.add_user()
        run(
            _router(_completion()).complete(
                "chat",
                [Message(role="user", content="hi")],
                group_id=world.group_id,
                user_id=user.user_id,
            )
        )
        assert world.count("llm_usage") == 1

    def test_row_captures_tokens_and_cost(
        self, run: Callable[[Coroutine[Any, Any, Any]], Any], world: World, pg: ModuleType
    ) -> None:
        run(
            _router(_completion()).complete(
                "chat", [Message(role="user", content="hi")], group_id=world.group_id
            )
        )
        row = run(pg.fetchrow("SELECT * FROM llm_usage WHERE group_id = $1", world.group_id))
        assert row["input_tokens"] == 120
        assert row["output_tokens"] == 45
        assert row["cache_read_tokens"] == 80
        assert float(row["cost_usd"]) == pytest.approx(0.0031)
        assert row["provider"] == "stub"
        assert row["outcome"] == "ok"

    def test_usage_id_is_uuid7(
        self, run: Callable[[Coroutine[Any, Any, Any]], Any], world: World, pg: ModuleType
    ) -> None:
        run(
            _router(_completion()).complete(
                "chat", [Message(role="user", content="hi")], group_id=world.group_id
            )
        )
        row = run(pg.fetchrow("SELECT usage_id FROM llm_usage WHERE group_id = $1", world.group_id))
        assert row["usage_id"].version == 7

    def test_refusal_is_recorded_as_such(
        self, run: Callable[[Coroutine[Any, Any, Any]], Any], world: World, pg: ModuleType
    ) -> None:
        run(
            _router(_completion(text="", stop_reason="refusal", refusal_category="cyber")).complete(
                "chat", [Message(role="user", content="x")], group_id=world.group_id
            )
        )
        row = run(pg.fetchrow("SELECT outcome FROM llm_usage WHERE group_id = $1", world.group_id))
        assert row["outcome"] == "refusal"

    def test_no_group_means_no_row(
        self, run: Callable[[Coroutine[Any, Any, Any]], Any], world: World
    ) -> None:
        """DM traffic has no group to attribute to; it must not write a row."""
        run(_router(_completion()).complete("chat", [Message(role="user", content="hi")]))
        assert world.count("llm_usage") == 0

    def test_unknown_price_stores_null_not_zero(
        self, run: Callable[[Coroutine[Any, Any, Any]], Any], world: World, pg: ModuleType
    ) -> None:
        """A missing price must not masquerade as free."""
        run(
            _router(_completion(cost_usd=None)).complete(
                "chat", [Message(role="user", content="hi")], group_id=world.group_id
            )
        )
        row = run(pg.fetchrow("SELECT cost_usd FROM llm_usage WHERE group_id = $1", world.group_id))
        assert row["cost_usd"] is None


class TestRollup:
    def test_daily_rollup_aggregates_per_model(
        self, run: Callable[[Coroutine[Any, Any, Any]], Any], world: World, pg: ModuleType
    ) -> None:
        router = _router(_completion())
        for _ in range(3):
            run(
                router.complete(
                    "chat", [Message(role="user", content="hi")], group_id=world.group_id
                )
            )

        today = dt.date.today()
        run(pg.execute("SELECT cb_rollup_llm_day($1)", today))

        row = run(
            pg.fetchrow(
                "SELECT * FROM llm_daily_cost WHERE group_id = $1 AND day = $2",
                world.group_id,
                today,
            )
        )
        assert row is not None
        assert row["calls"] == 3
        assert row["input_tokens"] == 360
        assert row["output_tokens"] == 135
        assert float(row["cost_usd"]) == pytest.approx(0.0093, abs=1e-4)

    def test_rollup_is_idempotent(
        self, run: Callable[[Coroutine[Any, Any, Any]], Any], world: World, pg: ModuleType
    ) -> None:
        run(
            _router(_completion()).complete(
                "chat", [Message(role="user", content="hi")], group_id=world.group_id
            )
        )
        today = dt.date.today()
        run(pg.execute("SELECT cb_rollup_llm_day($1)", today))
        run(pg.execute("SELECT cb_rollup_llm_day($1)", today))
        row = run(
            pg.fetchrow(
                "SELECT calls FROM llm_daily_cost WHERE group_id = $1 AND day = $2",
                world.group_id,
                today,
            )
        )
        assert row["calls"] == 1
