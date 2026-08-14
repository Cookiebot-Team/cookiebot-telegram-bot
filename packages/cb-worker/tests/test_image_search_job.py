"""Unit coverage for `cb_worker.jobs.image_search` — the Google Custom Search
call and the send loop that follows it. No Telegram session, no real network:
`httpx.MockTransport` through `image_search.set_http_client`, the same pattern
`test_youtube_job.py` uses for the same shape of job.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from cb_worker.jobs import image_search as job


@pytest.fixture(autouse=True)
def _reset_client() -> Iterator[None]:
    job.set_http_client(None)
    yield
    job.set_http_client(None)


def _transport(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _fake_settings(
    *, api_key: str = "test-key", cx: str = "test-cx", timeout: float = 5.0
) -> SimpleNamespace:
    return SimpleNamespace(
        google_search_api_key=api_key,
        google_search_cx=cx,
        google_search_timeout_seconds=timeout,
    )


def _bot(**overrides: Any) -> AsyncMock:
    bot = AsyncMock()
    for name, value in overrides.items():
        setattr(bot, name, value)
    return bot


def _item(url: str, referrer: str = "https://example.com/page") -> dict[str, Any]:
    return {"link": url, "image": {"contextLink": referrer}}


_RESPONSE = {"items": [_item("https://cdn.example.com/cat.jpg")]}


class TestIsAnimation:
    def test_a_gif_url(self) -> None:
        assert job.is_animation("https://cdn.example.com/cat.gif") is True

    def test_v1s_substring_wart_is_preserved(self) -> None:
        """v1 tests `'gif' in image.url` against the whole URL (`:161`), so a
        PNG under a `/gifts/` path is sent as an animation. Telegram delivers
        it either way."""
        assert job.is_animation("https://example.com/gifts/cat.png") is True

    def test_an_ordinary_photo_url(self) -> None:
        assert job.is_animation("https://cdn.example.com/cat.jpg") is False


class TestSearch:
    async def test_missing_credentials_make_no_request(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(job, "get_settings", lambda: _fake_settings(api_key=""))
        made_request = False

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal made_request
            made_request = True
            return httpx.Response(200, json=_RESPONSE)

        job.set_http_client(_transport(handler))
        assert await job._search("cat", safe="medium") is None  # noqa: SLF001
        assert made_request is False

    async def test_missing_cx_makes_no_request(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(job, "get_settings", lambda: _fake_settings(cx=""))
        job.set_http_client(_transport(lambda r: httpx.Response(200, json=_RESPONSE)))
        assert await job._search("cat", safe="medium") is None  # noqa: SLF001

    async def test_sends_v1s_query_parameters(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """v1: `{'q': term, 'num': 10, 'safe': ..., 'filetype': 'jpg|gif|png'}`
        (`SocialContent.py:154,156`) — plus the `searchType=image` its wrapper
        always set."""
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(dict(request.url.params))
            return httpx.Response(200, json=_RESPONSE)

        monkeypatch.setattr(job, "get_settings", _fake_settings)
        job.set_http_client(_transport(handler))
        await job._search(" cat ", safe="off")  # noqa: SLF001

        assert seen["q"] == " cat "
        assert seen["num"] == "10"
        assert seen["safe"] == "off"
        assert seen["fileType"] == "jpg|gif|png"
        assert seen["searchType"] == "image"

    async def test_empty_items_is_a_list_not_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(job, "get_settings", _fake_settings)
        job.set_http_client(_transport(lambda r: httpx.Response(200, json={})))
        assert await job._search("cat", safe="off") == []  # noqa: SLF001

    async def test_non_2xx_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(job, "get_settings", _fake_settings)
        job.set_http_client(_transport(lambda r: httpx.Response(429, json={"error": "quota"})))
        assert await job._search("cat", safe="off") is None  # noqa: SLF001

    async def test_timeout_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(job, "get_settings", _fake_settings)

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("timed out")

        job.set_http_client(_transport(handler))
        assert await job._search("cat", safe="off") is None  # noqa: SLF001


class TestRun:
    async def test_a_photo_result_is_sent_with_the_referrer_as_caption(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(job, "_search", AsyncMock(return_value=[_item("https://x/cat.jpg")]))
        bot = _bot(send_photo=AsyncMock(), send_chat_action=AsyncMock())

        await job._run(bot, 555, 42, " cat", "medium", "en")  # noqa: SLF001

        bot.send_chat_action.assert_awaited_once_with(555, "upload_photo")
        bot.send_photo.assert_awaited_once()
        args, kwargs = bot.send_photo.await_args
        assert args == (555, "https://x/cat.jpg")
        assert kwargs["caption"] == "https://example.com/page"
        assert kwargs["reply_to_message_id"] == 42

    async def test_a_gif_result_goes_through_send_animation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(job, "_search", AsyncMock(return_value=[_item("https://x/cat.gif")]))
        bot = _bot(send_animation=AsyncMock(), send_photo=AsyncMock(), send_chat_action=AsyncMock())

        await job._run(bot, 555, 42, " cat", "medium", "en")  # noqa: SLF001

        bot.send_animation.assert_awaited_once()
        bot.send_photo.assert_not_awaited()

    async def test_a_failing_result_falls_through_to_the_next(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """v1 wraps each send in its own `try` and moves on (`:159-166`): a
        result URL is a third-party page's image and may 404 or block
        hotlinking."""
        monkeypatch.setattr(
            job,
            "_search",
            AsyncMock(return_value=[_item("https://x/one.jpg"), _item("https://x/two.jpg")]),
        )
        sends: list[str] = []

        async def send_photo(chat_id: int, url: str, **kwargs: Any) -> None:
            sends.append(url)
            if len(sends) == 1:
                raise RuntimeError("Telegram could not fetch it")

        bot = _bot(send_photo=send_photo, send_chat_action=AsyncMock())
        # A fixed shuffle so "the first one fails" is deterministic.
        monkeypatch.setattr(job.random, "shuffle", lambda items: None)

        await job._run(bot, 555, 42, " cat", "medium", "en")  # noqa: SLF001

        assert sends == ["https://x/one.jpg", "https://x/two.jpg"]

    async def test_every_result_failing_reacts_and_says_not_found(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(job, "_search", AsyncMock(return_value=[_item("https://x/one.jpg")]))
        bot = _bot(
            send_photo=AsyncMock(side_effect=RuntimeError("nope")),
            send_message=AsyncMock(),
            set_message_reaction=AsyncMock(),
            send_chat_action=AsyncMock(),
        )

        await job._run(bot, 555, 42, " cat", "medium", "en")  # noqa: SLF001

        bot.set_message_reaction.assert_awaited_once()
        bot.send_message.assert_awaited_once()
        assert bot.send_message.await_args.kwargs["reply_to_message_id"] == 42

    async def test_no_results_reacts_and_says_not_found(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(job, "_search", AsyncMock(return_value=[]))
        bot = _bot(
            send_message=AsyncMock(), set_message_reaction=AsyncMock(), send_chat_action=AsyncMock()
        )
        before = job.image_search_total.labels(outcome="not_found")._value.get()  # noqa: SLF001

        await job._run(bot, 555, 42, " cat", "medium", "en")  # noqa: SLF001

        bot.send_message.assert_awaited_once()
        assert job.image_search_total.labels(outcome="not_found")._value.get() == before + 1  # noqa: SLF001

    async def test_a_request_failure_counts_as_error_but_still_replies(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(job, "_search", AsyncMock(return_value=None))
        bot = _bot(
            send_message=AsyncMock(), set_message_reaction=AsyncMock(), send_chat_action=AsyncMock()
        )
        before = job.image_search_total.labels(outcome="error")._value.get()  # noqa: SLF001

        await job._run(bot, 555, 42, " cat", "medium", "en")  # noqa: SLF001

        bot.send_message.assert_awaited_once()
        assert job.image_search_total.labels(outcome="error")._value.get() == before + 1  # noqa: SLF001

    async def test_a_reaction_failure_does_not_abort_the_reply(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(job, "_search", AsyncMock(return_value=[]))
        bot = _bot(
            send_message=AsyncMock(),
            set_message_reaction=AsyncMock(side_effect=Exception("Telegram is down")),
            send_chat_action=AsyncMock(),
        )
        await job._run(bot, 555, 42, " cat", "medium", "en")  # noqa: SLF001
        bot.send_message.assert_awaited_once()
