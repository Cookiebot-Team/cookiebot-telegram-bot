"""Step definitions for x_reverse_search.

QA: qa/features/x_reverse_search.feature — **authored**, no upstream scenario
exists. Contract: docs/contracts/x_reverse_search.md.

Drives the real dispatcher against the mock Telegram API. Two things are
mocked, both the outside world (AGENTS.md §6): the arq broker (replaced in the
handler's own namespace, then the recorded job is run inline so the scenario
asserts what it actually did) and SauceNAO, through
`publisher`-style `set_http_client`.
"""

from __future__ import annotations

import io
import time
from collections.abc import Callable, Coroutine, Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from aiogram import Bot, Dispatcher
from pytest_bdd import given, scenarios, then, when

from cb_core import group_config, locales
from cb_gateway.handlers import reverse_search as handler
from cb_worker.jobs import reverse_search as job
from qa.conftest import GROUP_ID, USER_ID, Context, feed, make_message_update, next_update_id
from qa.mock_telegram import MockTelegram

scenarios("x_reverse_search.feature")

Run = Callable[[Coroutine[Any, Any, Any]], Any]


class Ctx:
    def __init__(self) -> None:
        self.payload: dict[str, Any] | None = None
        self.bot: AsyncMock | None = None
        self.searched: bool = False


@pytest.fixture
def rs_ctx() -> Ctx:
    return Ctx()


@pytest.fixture(autouse=True)
def fake_queue(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def _fake_enqueue(name: str, *args: object, **kwargs: object) -> bool:
        calls.append((name, dict(kwargs)))  # type: ignore[arg-type]
        return True

    monkeypatch.setattr(handler, "enqueue", _fake_enqueue)
    return calls


@pytest.fixture(autouse=True)
def _saucenao(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(
        job,
        "get_settings",
        lambda: SimpleNamespace(saucenao_api_key="k", saucenao_timeout_seconds=15.0),
    )
    yield
    job.set_http_client(None)


@pytest.fixture(autouse=True)
def _reset_config(database: Any, run: Run) -> Iterator[None]:
    """`functions_utility` is written by one scenario and read from a
    process-global L1 by the rest, so it has to be put back. Takes `database`
    so the whole suite skips cleanly when Postgres is unreachable rather than
    failing in teardown."""
    yield
    run(group_config.set_config(GROUP_ID, functions_utility=True))
    group_config._l1.clear()  # noqa: SLF001 - process-global


def _install(rs_ctx: Ctx, payload: dict[str, Any]) -> None:
    def respond(_: httpx.Request) -> httpx.Response:
        rs_ctx.searched = True
        return httpx.Response(200, json=payload)

    job.set_http_client(httpx.AsyncClient(transport=httpx.MockTransport(respond)))


def _picture(message_id: int) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "date": int(time.time()),
        "chat": {"id": GROUP_ID, "type": "supergroup", "title": "QA Group"},
        "from": {"id": USER_ID, "is_bot": False, "first_name": "Tester"},
        "photo": [
            {
                "file_id": "pic-1",
                "file_unique_id": "up1",
                "width": 90,
                "height": 90,
                "file_size": 1,
            }
        ],
    }


def _plain(message_id: int) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "date": int(time.time()),
        "chat": {"id": GROUP_ID, "type": "supergroup", "title": "QA Group"},
        "from": {"id": USER_ID, "is_bot": False, "first_name": "Tester"},
        "text": "no picture here",
    }


def _run_job(run: Run, rs_ctx: Ctx, queue: list[tuple[str, dict[str, Any]]]) -> None:
    """Run whatever the handler enqueued, against a mock bot."""
    if not queue:
        return
    _name, kwargs = queue[-1]
    rs_ctx.bot = AsyncMock()
    rs_ctx.bot.download.return_value = io.BytesIO(b"\xff\xd8jpeg")
    run(job._run(rs_ctx.bot, **kwargs))  # noqa: SLF001


# --------------------------------------------------------------------- given


@given("that the bot is in the group and properly set up")
def bot_set_up(ctx: Context) -> None:
    ctx.bot_running = True


@given("the search will find a match")
def will_match(rs_ctx: Ctx) -> None:
    _install(
        rs_ctx,
        {
            "header": {"short_remaining": 4, "long_remaining": 90},
            "results": [
                {
                    "header": {"similarity": "94.2"},
                    "data": {
                        "title": "Sunset Study",
                        "author_name": "Ana",
                        "ext_urls": ["https://gallery.example/art/1"],
                    },
                }
            ],
        },
    )


@given("the search will find nothing")
def will_not_match(rs_ctx: Ctx) -> None:
    _install(rs_ctx, {"header": {"short_remaining": 4, "long_remaining": 90}, "results": []})


@given("the daily search limit has been reached")
def limit_reached(rs_ctx: Ctx) -> None:
    """v1's `LongLimitReachedError` branch (`SocialContent.py:125-128`)."""
    _install(rs_ctx, {"header": {"short_remaining": 4, "long_remaining": -1}, "results": []})


@given("that utility functions are disabled for the group")
def utility_off(run: Run) -> None:
    run(group_config.set_config(GROUP_ID, functions_utility=False))
    group_config._l1.clear()  # noqa: SLF001


# ---------------------------------------------------------------------- when


@when("the user sends /buscarfonte without replying to anything")
def no_reply(run: Run, dispatcher: Dispatcher, bot: Bot) -> None:
    feed(run, dispatcher, bot, make_message_update("/buscarfonte", next_update_id()))


@when("the user replies /buscarfonte to a plain text message")
def reply_to_text(run: Run, dispatcher: Dispatcher, bot: Bot) -> None:
    update_id = next_update_id()
    feed(
        run,
        dispatcher,
        bot,
        make_message_update("/buscarfonte", update_id, reply_to=_plain(update_id - 1)),
    )


@when("the user replies /buscarfonte to a picture")
def reply_to_picture(
    run: Run,
    dispatcher: Dispatcher,
    bot: Bot,
    rs_ctx: Ctx,
    fake_queue: list[tuple[str, dict[str, Any]]],
) -> None:
    update_id = next_update_id()
    feed(
        run,
        dispatcher,
        bot,
        make_message_update("/buscarfonte", update_id, reply_to=_picture(update_id - 1)),
    )
    _run_job(run, rs_ctx, fake_queue)


# ---------------------------------------------------------------------- then


@then('the bot answers with the "reply an image" instructions')
def instructions(telegram: MockTelegram) -> None:
    sent = telegram.calls_to("sendMessage")
    assert sent, "expected the instructions"
    assert sent[-1]["text"] == locales.get("reverse_image", "en")


@then("the bot replies with the title, the author and the source link")
def reports_the_match(rs_ctx: Ctx) -> None:
    assert rs_ctx.bot is not None
    text = rs_ctx.bot.send_message.await_args.args[1]
    assert locales.get("reverse_best", "en") in text
    assert '"Sunset Study"' in text
    assert " - Ana" in text
    assert "https://gallery.example/art/1" in text
    emoji = rs_ctx.bot.set_message_reaction.await_args.kwargs["reaction"][0].emoji
    assert emoji == "🫡"


@then("the bot replies that the image seems to be original")
def reports_no_match(rs_ctx: Ctx) -> None:
    assert rs_ctx.bot is not None
    assert rs_ctx.bot.send_message.await_args.args[1] == locales.get("reverse_no_found", "en")
    emoji = rs_ctx.bot.set_message_reaction.await_args.kwargs["reaction"][0].emoji
    assert emoji == "🤷"


@then("the bot replies that the daily limit was reached")
def reports_the_limit(rs_ctx: Ctx) -> None:
    assert rs_ctx.bot is not None
    assert rs_ctx.bot.send_message.await_args.args[1] == locales.get("reverse_limit", "en")
    # v1 returns before the reaction on both limit branches.
    rs_ctx.bot.set_message_reaction.assert_not_awaited()


@then("the bot says the utility functions are off")
def utility_off_reply(telegram: MockTelegram) -> None:
    """v1 answers a gated-off *command* rather than ignoring it
    (`notify_utility_off`, `COOKIEBOT.py:252`)."""
    sent = telegram.calls_to("sendMessage")
    assert sent, "expected the utility_off notice"
    assert sent[-1]["text"] == locales.get("utility_off", "en")


@then("no search is performed")
def nothing_searched(rs_ctx: Ctx, fake_queue: list[tuple[str, dict[str, Any]]]) -> None:
    assert fake_queue == [], "the gate must refuse before enqueuing"
    assert rs_ctx.searched is False
