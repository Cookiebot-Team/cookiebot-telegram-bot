"""Step definitions for x_image_search.

QA: qa/features/x_image_search.feature (authored locally — see the feature
file's header). Contract: docs/contracts/x_image_search.md.

Two seams, both "mock the outside world" (AGENTS.md §6): the arq broker, the
same `enqueue` monkeypatch `qa/test_util_youtube.py` uses for its own worker
half, and the quota's Valkey counter, which is the only reason a scenario
could otherwise leak into the next one — the keys are per-day, not
per-scenario.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Coroutine, Iterator
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from pytest_bdd import given, parsers, scenarios, then, when

from cb_core import group_config, jobs, locales
from cb_gateway.handlers import image_search as handler
from qa.conftest import GROUP_ID, USER_ID, Context, feed, make_message_update, next_update_id
from qa.mock_telegram import MockTelegram

scenarios("x_image_search.feature")


@pytest.fixture(autouse=True)
def fake_queue(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def _fake_enqueue(job: str, *args: object, **kwargs: object) -> bool:
        calls.append((job, kwargs))
        return True

    monkeypatch.setattr(handler, "enqueue", _fake_enqueue)
    return calls


class Ctx:
    def __init__(self) -> None:
        self.exhausted = False

    def alloc_id(self) -> int:
        return next_update_id()


@pytest.fixture
def search_ctx() -> Ctx:
    return Ctx()


@pytest.fixture(autouse=True)
def fake_quota(search_ctx: Ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    """The quota is a shared Valkey counter keyed by day, so a real one would
    carry between scenarios and between runs. The *rule* it implements is
    unit-tested (`packages/cb-gateway/tests/test_image_search.py`); what these
    scenarios need is only "under" or "over"."""

    async def _within_quota(user_id: int) -> bool:
        return not search_ctx.exhausted

    monkeypatch.setattr(handler, "within_quota", _within_quota)


@pytest.fixture(autouse=True)
def _restore_config(run: Callable[[Coroutine[Any, Any, Any]], Any]) -> Iterator[None]:
    yield
    with contextlib.suppress(Exception):
        run(group_config.set_config(GROUP_ID, functions_utility=True, sfw=True))
    group_config._l1.clear()  # noqa: SLF001


# --------------------------------------------------------------------- given


@given("that the bot is in the group and properly set up")
def bot_set_up(ctx: Context) -> None:
    ctx.bot_running = True


@given("that the user is a member of the group")
def user_is_member() -> None:
    """Nothing to arrange — the catch-all reads no registry."""


@given("the group is configured as safe-for-work")
def group_is_sfw(database: Any, run: Callable[[Coroutine[Any, Any, Any]], Any]) -> None:
    run(group_config.set_config(GROUP_ID, sfw=True))
    group_config._l1.clear()  # noqa: SLF001


@given("the group is not configured as safe-for-work")
def group_is_not_sfw(database: Any, run: Callable[[Coroutine[Any, Any, Any]], Any]) -> None:
    run(group_config.set_config(GROUP_ID, sfw=False))
    group_config._l1.clear()  # noqa: SLF001


@given("the utility feature is turned off")
def utility_disabled(database: Any, run: Callable[[Coroutine[Any, Any, Any]], Any]) -> None:
    run(group_config.set_config(GROUP_ID, functions_utility=False))
    group_config._l1.clear()  # noqa: SLF001


@given("the user has used up today's image searches")
def quota_exhausted(search_ctx: Ctx) -> None:
    search_ctx.exhausted = True


# ---------------------------------------------------------------------- when


@when(parsers.parse('the user types the command "{text}"'))
def user_types(
    search_ctx: Ctx,
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
    text: str,
) -> None:
    feed(run, dispatcher, bot, make_message_update(text, search_ctx.alloc_id(), user_id=USER_ID))


# ---------------------------------------------------------------------- then


def _last_text(telegram: MockTelegram) -> str:
    sent = telegram.calls_to("sendMessage")
    assert sent, f"expected a sendMessage call, got {telegram.calls}"
    return str(sent[-1].get("text", ""))


@then("the bot should reply with the usage example")
def bot_replies_with_usage(
    telegram: MockTelegram, fake_queue: list[tuple[str, dict[str, Any]]]
) -> None:
    """`/anything` never searches: it prints the example and stops
    (`SocialContent.py:144-146`)."""
    assert _last_text(telegram) == locales.get("anything_prompt", "en")
    assert not fake_queue


@then(parsers.parse('the bot should queue an image search for "{query}"'))
def bot_queues_a_search(fake_queue: list[tuple[str, dict[str, Any]]], query: str) -> None:
    assert len(fake_queue) == 1, fake_queue
    job, kwargs = fake_queue[0]
    assert job == jobs.IMAGE_SEARCH
    assert kwargs["group_id"] == GROUP_ID
    # The leading space is v1's: `text.replace("/", " ")` (`:148`).
    assert kwargs["query"] == query


@then("the queued search should have safe search on")
def queued_search_is_safe(fake_queue: list[tuple[str, dict[str, Any]]]) -> None:
    """v1: `safe='medium'` for an SFW group (`SocialContent.py:156`)."""
    assert fake_queue[0][1]["safe"] == "medium"


@then("the queued search should have safe search off")
def queued_search_is_unsafe(fake_queue: list[tuple[str, dict[str, Any]]]) -> None:
    assert fake_queue[0][1]["safe"] == "off"


@then("the bot should queue nothing and say nothing")
def bot_does_nothing(telegram: MockTelegram, fake_queue: list[tuple[str, dict[str, Any]]]) -> None:
    assert not fake_queue
    assert not telegram.calls_to("sendMessage")
    assert not telegram.calls_to("sendPhoto")


@then("the bot should answer the real command and queue nothing")
def real_command_still_answers(
    telegram: MockTelegram, fake_queue: list[tuple[str, dict[str, Any]]]
) -> None:
    """The regression this feature is capable of causing: a catch-all that
    returns instead of raising `SkipHandler` swallows every command whose
    router is registered after it."""
    assert not fake_queue
    assert telegram.calls_to("sendMessage"), "the real command answered nothing"


@then("the bot should reply that the image search limit is reached")
def bot_says_limit(telegram: MockTelegram, fake_queue: list[tuple[str, dict[str, Any]]]) -> None:
    assert _last_text(telegram) == locales.get("image_limit", "en")
    assert not fake_queue
