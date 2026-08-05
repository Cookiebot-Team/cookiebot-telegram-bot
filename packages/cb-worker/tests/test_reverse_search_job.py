"""Unit coverage for `cb_worker.jobs.reverse_search`.

No Telegram session and no real network: the bot is an `AsyncMock` and SauceNAO
goes through `httpx.MockTransport` via `set_http_client`, the pattern
`test_youtube_job.py` established. Contract:
`docs/contracts/x_reverse_search.md`.
"""

from __future__ import annotations

import io
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from cb_core import locales
from cb_worker.jobs import reverse_search as job


@pytest.fixture(autouse=True)
def _reset_client() -> Iterator[None]:
    job.set_http_client(None)
    yield
    job.set_http_client(None)


def _transport(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _settings(**overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = {"saucenao_api_key": "k", "saucenao_timeout_seconds": 15.0}
    base.update(overrides)
    return SimpleNamespace(**base)


def _bot() -> AsyncMock:
    bot = AsyncMock()
    bot.download.return_value = io.BytesIO(b"\xff\xd8jpegbytes")
    return bot


def _response(
    *, similarity: str = "95.5", urls: list[str] | None = None, **data: Any
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "title": "A Painting",
        # `urls or [...]` would turn an explicitly empty list back into the
        # default, which is the case one of these tests is about.
        "ext_urls": ["https://src/1"] if urls is None else urls,
    }
    payload.update(data)
    return {
        "header": {"short_remaining": 4, "long_remaining": 90},
        "results": [{"header": {"similarity": similarity}, "data": payload}],
    }


def _ok(payload: dict[str, Any]) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _: httpx.Response(200, json=payload)


# --------------------------------------------------------------- D-RS-1's fix


@pytest.mark.asyncio
async def test_the_bot_token_is_never_sent_to_saucenao(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole reason this feature is a worker job.

    v1 builds `https://api.telegram.org/file/bot{TOKEN}/{path}` and hands that
    URL to SauceNAO (`SocialContent.py:89,119-120`), so the bot token lands in
    a third party's access logs. Anyone holding it controls the bot.

    This asserts the shape that makes the leak impossible: the image travels as
    an uploaded file part, and no `url` field is sent at all. Reintroducing
    `url=` fails here.
    """
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["content"] = request.content
        seen["url"] = str(request.url)
        return httpx.Response(200, json=_response())

    job.set_http_client(_transport(handler))
    monkeypatch.setattr(job, "get_settings", _settings)
    await job._search(b"\xff\xd8jpegbytes")  # noqa: SLF001

    body = seen["content"]
    assert b'name="file"' in body, "the image must be uploaded, not linked"
    assert b'name="url"' not in body, "sending a url is how v1 leaked the token"
    assert b"api.telegram.org" not in body
    assert b"bot" not in seen["url"].encode()


# ------------------------------------------------------------------- the search


@pytest.mark.asyncio
async def test_a_confident_hit_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    job.set_http_client(_transport(_ok(_response(author_name="Ana"))))
    monkeypatch.setattr(job, "get_settings", _settings)
    result = await job._search(b"x")  # noqa: SLF001
    assert result.kind == "found"
    assert result.title == "A Painting"
    assert result.author == "Ana"
    assert result.url == "https://src/1"


@pytest.mark.parametrize(
    ("similarity", "expected"),
    [
        ("80.1", "found"),
        # v1's comparison is `> 80`, strictly — 80.0 exactly is a miss.
        ("80.0", "not_found"),
        ("79.9", "not_found"),
    ],
)
@pytest.mark.asyncio
async def test_the_threshold_is_strictly_greater_than_80(
    monkeypatch: pytest.MonkeyPatch, similarity: str, expected: str
) -> None:
    job.set_http_client(_transport(_ok(_response(similarity=similarity))))
    monkeypatch.setattr(job, "get_settings", _settings)
    assert (await job._search(b"x")).kind == expected  # noqa: SLF001


@pytest.mark.asyncio
async def test_a_hit_with_no_urls_is_a_miss(monkeypatch: pytest.MonkeyPatch) -> None:
    """v1: `results[0].urls and ...` (`:129`)."""
    job.set_http_client(_transport(_ok(_response(urls=[]))))
    monkeypatch.setattr(job, "get_settings", _settings)
    assert (await job._search(b"x")).kind == "not_found"  # noqa: SLF001


@pytest.mark.parametrize("key", ["author_name", "member_name", "creator", "artist"])
@pytest.mark.asyncio
async def test_the_author_is_found_under_any_index_specific_key(
    monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    """`saucenao_api` normalises the author across indexes; v2 must too, or an
    author v1 displayed silently disappears."""
    job.set_http_client(_transport(_ok(_response(**{key: "Ana"}))))
    monkeypatch.setattr(job, "get_settings", _settings)
    assert (await job._search(b"x")).author == "Ana"  # noqa: SLF001


@pytest.mark.asyncio
async def test_a_collaborator_list_takes_the_first(monkeypatch: pytest.MonkeyPatch) -> None:
    job.set_http_client(_transport(_ok(_response(creator=["Ana", "Bo"]))))
    monkeypatch.setattr(job, "get_settings", _settings)
    assert (await job._search(b"x")).author == "Ana"  # noqa: SLF001


# ------------------------------------------------------------------ rate limits


@pytest.mark.asyncio
async def test_short_limit_is_distinguished(monkeypatch: pytest.MonkeyPatch) -> None:
    """v1's `ShortLimitReachedError` branch (`:121-124`), which
    `saucenao_api` raises off exactly this field."""
    payload = _response()
    payload["header"]["short_remaining"] = -1
    job.set_http_client(_transport(_ok(payload)))
    monkeypatch.setattr(job, "get_settings", _settings)
    assert (await job._search(b"x")).kind == "short_limit"  # noqa: SLF001


@pytest.mark.asyncio
async def test_long_limit_is_distinguished(monkeypatch: pytest.MonkeyPatch) -> None:
    """v1's `LongLimitReachedError` branch (`:125-128`)."""
    payload = _response()
    payload["header"]["long_remaining"] = -1
    job.set_http_client(_transport(_ok(payload)))
    monkeypatch.setattr(job, "get_settings", _settings)
    assert (await job._search(b"x")).kind == "long_limit"  # noqa: SLF001


@pytest.mark.asyncio
async def test_short_limit_wins_when_both_are_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v1 catches `ShortLimitReachedError` first (`:121` before `:125`)."""
    payload = _response()
    payload["header"] = {"short_remaining": -1, "long_remaining": -1}
    job.set_http_client(_transport(_ok(payload)))
    monkeypatch.setattr(job, "get_settings", _settings)
    assert (await job._search(b"x")).kind == "short_limit"  # noqa: SLF001


# ------------------------------------------------------------------- degradation


@pytest.mark.parametrize(
    "handler",
    [
        lambda _: httpx.Response(500),
        lambda _: httpx.Response(403),
        lambda _: httpx.Response(200, content=b"not json"),
        lambda _: httpx.Response(200, json=["a", "list"]),
        lambda _: httpx.Response(200, json={"header": {}, "results": []}),
    ],
    ids=["5xx", "forbidden", "malformed", "wrong-shape", "empty"],
)
@pytest.mark.asyncio
async def test_every_failure_degrades_to_not_found(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> None:
    """D-RS-3. v1 lets anything but the two rate limits propagate into the
    global traceback handler, so an outage is silence in the group."""
    job.set_http_client(_transport(handler))
    monkeypatch.setattr(job, "get_settings", _settings)
    assert (await job._search(b"x")).kind == "not_found"  # noqa: SLF001


@pytest.mark.asyncio
async def test_a_timeout_degrades_to_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("slow")

    job.set_http_client(_transport(handler))
    monkeypatch.setattr(job, "get_settings", _settings)
    assert (await job._search(b"x")).kind == "not_found"  # noqa: SLF001


@pytest.mark.asyncio
async def test_no_api_key_makes_no_request(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(_: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError("no request should be made without a key")

    job.set_http_client(_transport(handler))
    monkeypatch.setattr(job, "get_settings", lambda: _settings(saucenao_api_key=""))
    assert (await job._search(b"x")).kind == "not_found"  # noqa: SLF001


# ----------------------------------------------------------------- the answer


def test_the_answer_is_assembled_exactly_as_v1_does() -> None:
    """`:131-136`, trailing newlines included."""
    out = job.build_answer(
        job._Outcome("found", title="A Painting", author="Ana", url="https://src/1"),  # noqa: SLF001
        "en",
    )
    expected = locales.get("reverse_best", "en") + '"A Painting" - Ana\nhttps://src/1\n\n'
    assert out == expected


def test_an_absent_author_drops_the_dash() -> None:
    """v1 appends `f" - {author}"` only when there is one (`:133-134`)."""
    out = job.build_answer(
        job._Outcome("found", title="A Painting", url="https://src/1"),  # noqa: SLF001
        "en",
    )
    assert " - " not in out


# ---------------------------------------------------------------------- the run


@pytest.mark.asyncio
async def test_a_hit_reacts_then_replies(monkeypatch: pytest.MonkeyPatch) -> None:
    job.set_http_client(_transport(_ok(_response())))
    monkeypatch.setattr(job, "get_settings", _settings)
    bot = _bot()
    await job._run(bot, -1001, 7, "file-1", "en")  # noqa: SLF001

    bot.set_message_reaction.assert_awaited_once()
    assert bot.set_message_reaction.await_args.kwargs["is_big"] is False
    assert bot.send_message.await_args.args[1].startswith("Best match found:")
    assert bot.send_message.await_args.kwargs["reply_to_message_id"] == 7


@pytest.mark.asyncio
async def test_a_miss_reacts_with_the_shrug(monkeypatch: pytest.MonkeyPatch) -> None:
    job.set_http_client(_transport(_ok(_response(similarity="10"))))
    monkeypatch.setattr(job, "get_settings", _settings)
    bot = _bot()
    await job._run(bot, -1001, 7, "file-1", "en")  # noqa: SLF001

    emoji = bot.set_message_reaction.await_args.kwargs["reaction"][0].emoji
    assert emoji == "🤷"
    assert "no matches" in bot.send_message.await_args.args[1]


@pytest.mark.asyncio
async def test_a_rate_limit_neither_reacts_nor_searches_further(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v1 returns before the reaction on both limit branches (`:121-128`)."""
    payload = _response()
    payload["header"]["long_remaining"] = -1
    job.set_http_client(_transport(_ok(payload)))
    monkeypatch.setattr(job, "get_settings", _settings)
    bot = _bot()
    await job._run(bot, -1001, 7, "file-1", "en")  # noqa: SLF001

    bot.set_message_reaction.assert_not_awaited()
    assert "Daily search limit" in bot.send_message.await_args.args[1]


@pytest.mark.asyncio
async def test_a_failed_download_still_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    """v1 has no branch here — `fetch_temp_jpg` raises and the update dies."""
    monkeypatch.setattr(job, "get_settings", _settings)
    bot = _bot()
    bot.download.side_effect = RuntimeError("no file access")
    await job._run(bot, -1001, 7, "file-1", "en")  # noqa: SLF001
    assert "no matches" in bot.send_message.await_args.args[1]


@pytest.mark.asyncio
async def test_the_reply_is_sent_without_html_parse_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The answer interpolates a SauceNAO title verbatim, so a `<` in it would
    be rejected as bad HTML. v1's `send_message` defaults to `parse_mode='HTML'`
    and loses exactly those replies."""
    job.set_http_client(_transport(_ok(_response(title="a <b> title & more"))))
    monkeypatch.setattr(job, "get_settings", _settings)
    bot = _bot()
    await job._run(bot, -1001, 7, "file-1", "en")  # noqa: SLF001
    assert "parse_mode" not in bot.send_message.await_args.kwargs
