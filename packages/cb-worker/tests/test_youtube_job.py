"""Unit coverage for `cb_worker.jobs.youtube` — the YouTube Data API call
and the reply it produces. No Telegram session, no real network: every call
goes through `httpx.MockTransport` via `youtube.set_http_client`, same
pattern `packages/cb-gateway/tests/test_doomlist.py` established for its own
external dependencies.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from cb_worker.jobs import youtube as job


@pytest.fixture(autouse=True)
def _reset_client() -> Iterator[None]:
    job.set_http_client(None)
    yield
    job.set_http_client(None)


def _transport(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _fake_settings(*, api_key: str = "test-key", timeout: float = 5.0) -> SimpleNamespace:
    return SimpleNamespace(youtube_api_key=api_key, youtube_timeout_seconds=timeout)


def _bot(**overrides: Any) -> AsyncMock:
    bot = AsyncMock()
    for name, value in overrides.items():
        setattr(bot, name, value)
    return bot


_SEARCH_RESPONSE = {
    "items": [
        {"id": {"videoId": "abc123"}, "snippet": {"description": "a cake tutorial"}},
        {"id": {"videoId": "def456"}, "snippet": {"description": "another cake tutorial"}},
    ]
}


class TestSearch:
    async def test_no_api_key_returns_none_without_a_request(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(job, "get_settings", lambda: _fake_settings(api_key=""))
        made_request = False

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal made_request
            made_request = True
            return httpx.Response(200, json=_SEARCH_RESPONSE)

        job.set_http_client(_transport(handler))
        assert await job._search("cake") is None  # noqa: SLF001
        assert made_request is False

    async def test_successful_response_returns_items(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(job, "get_settings", _fake_settings)
        job.set_http_client(_transport(lambda r: httpx.Response(200, json=_SEARCH_RESPONSE)))
        items = await job._search("cake")  # noqa: SLF001
        assert items == _SEARCH_RESPONSE["items"]

    async def test_empty_items_is_an_empty_list_not_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(job, "get_settings", _fake_settings)
        job.set_http_client(_transport(lambda r: httpx.Response(200, json={"items": []})))
        assert await job._search("cake") == []  # noqa: SLF001

    async def test_non_2xx_response_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(job, "get_settings", _fake_settings)
        job.set_http_client(_transport(lambda r: httpx.Response(403, json={"error": "bad key"})))
        assert await job._search("cake") is None  # noqa: SLF001

    async def test_timeout_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(job, "get_settings", _fake_settings)

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("timed out")

        job.set_http_client(_transport(handler))
        assert await job._search("cake") is None  # noqa: SLF001

    async def test_malformed_json_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(job, "get_settings", _fake_settings)
        job.set_http_client(_transport(lambda r: httpx.Response(200, text="not json")))
        assert await job._search("cake") is None  # noqa: SLF001

    async def test_items_not_a_list_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(job, "get_settings", _fake_settings)
        job.set_http_client(_transport(lambda r: httpx.Response(200, json={"items": "oops"})))
        assert await job._search("cake") is None  # noqa: SLF001


class TestRun:
    async def test_a_result_sends_the_video_link_as_a_reply(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(job, "_search", AsyncMock(return_value=[_SEARCH_RESPONSE["items"][0]]))
        bot = _bot(send_message=AsyncMock())
        await job._run(bot, 555, 42, "cake", "en")  # noqa: SLF001
        bot.send_message.assert_awaited_once()
        args, kwargs = bot.send_message.await_args
        assert args[0] == 555
        assert "abc123" in args[1]
        assert "a cake tutorial" in args[1]
        assert kwargs["parse_mode"] == "HTML"
        assert kwargs["reply_to_message_id"] == 42
        assert job.youtube_search_total.labels(outcome="sent")._value.get() >= 1  # noqa: SLF001

    async def test_empty_result_reacts_and_sends_not_found(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(job, "_search", AsyncMock(return_value=[]))
        bot = _bot(send_message=AsyncMock(), set_message_reaction=AsyncMock())
        before = job.youtube_search_total.labels(outcome="not_found")._value.get()  # noqa: SLF001

        await job._run(bot, 555, 42, "cake", "en")  # noqa: SLF001

        bot.set_message_reaction.assert_awaited_once()
        assert bot.set_message_reaction.await_args.args == (555, 42)
        bot.send_message.assert_awaited_once()
        assert bot.send_message.await_args.kwargs["reply_to_message_id"] == 42
        assert job.youtube_search_total.labels(outcome="not_found")._value.get() == before + 1  # noqa: SLF001

    async def test_request_failure_still_replies_not_found_but_counts_as_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(job, "_search", AsyncMock(return_value=None))
        bot = _bot(send_message=AsyncMock(), set_message_reaction=AsyncMock())
        before = job.youtube_search_total.labels(outcome="error")._value.get()  # noqa: SLF001

        await job._run(bot, 555, 42, "cake", "en")  # noqa: SLF001

        bot.send_message.assert_awaited_once()
        assert job.youtube_search_total.labels(outcome="error")._value.get() == before + 1  # noqa: SLF001

    async def test_reaction_failure_does_not_abort_the_reply(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(job, "_search", AsyncMock(return_value=[]))
        bot = _bot(
            send_message=AsyncMock(),
            set_message_reaction=AsyncMock(side_effect=Exception("Telegram is down")),
        )
        await job._run(bot, 555, 42, "cake", "en")  # noqa: SLF001
        bot.send_message.assert_awaited_once()


class TestSearchYoutubeWrapper:
    async def test_full_job_runs_without_raising_and_uses_the_context_bot(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(job, "_search", AsyncMock(return_value=[_SEARCH_RESPONSE["items"][0]]))
        bot = _bot(send_message=AsyncMock())
        ctx: dict[str, Any] = {"bot": bot}
        await job.search_youtube(ctx, group_id=555, message_id=42, query="cake", lang="en")
        bot.send_message.assert_awaited_once()
