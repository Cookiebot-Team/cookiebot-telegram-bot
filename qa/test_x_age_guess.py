"""Step definitions for x_age_guess.

QA: qa/features/x_age_guess.feature (authored here — Cookiebot-QA has no
scenario for this feature; see the file's own header).

agify.io is stubbed via `age.set_http_client` with an `httpx.MockTransport`,
the same boundary-stubbing idiom `qa/test_util_doomlist.py` uses for cas.chat
and burrbot — the outside world is faked, never our own code (AGENTS.md §6).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from cb_core import group_config, locales
from cb_gateway.handlers import age
from qa.conftest import GROUP_ID, feed, make_message_update

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Iterator

    from aiogram import Bot, Dispatcher

    from qa.conftest import Context
    from qa.mock_telegram import MockTelegram

scenarios("x_age_guess.feature")


def _stub_transport(*, age_value: int | None, count: int, down: bool = False) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if down:
            raise httpx.ConnectError("simulated agify.io outage", request=request)
        return httpx.Response(200, json={"name": "x", "age": age_value, "count": count})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def _reset_age_state() -> Iterator[None]:
    """Breaker and injected HTTP client are process-global — reset around
    every scenario, same reasoning as `qa/test_util_doomlist.py`'s own
    `_reset_doomlist_state`."""
    age._breaker = age.Breaker()  # noqa: SLF001
    yield
    age.set_http_client(None)


@given("that the bot is in the group and properly set up")
def bot_set_up(ctx: Context) -> None:
    ctx.bot_running = True


@given("that a user is in the group")
def user_in_group() -> None:
    pass


@given("agify.io is stubbed to answer normally")
def agify_default() -> None:
    age.set_http_client(_stub_transport(age_value=30, count=100))


@given(parsers.parse("that agify.io reports an age of {value:d} from {count:d} records"))
def agify_reports(value: int, count: int) -> None:
    age.set_http_client(_stub_transport(age_value=value, count=count))


@given("that agify.io reports zero records")
def agify_zero() -> None:
    age.set_http_client(_stub_transport(age_value=None, count=0))


@given("that agify.io is down")
def agify_down() -> None:
    age.set_http_client(_stub_transport(age_value=None, count=0, down=True))


@given("that fun functions are disabled for the group")
def fun_disabled(database: Any, run: Callable[[Coroutine[Any, Any, Any]], Any]) -> None:
    """Same reasoning as `fun_random`'s/`unearth`'s own step: the flag lives in
    `group_configs`, and `database` skips cleanly with no Postgres listening."""
    run(group_config.set_config(GROUP_ID, functions_fun=False))


@when(parsers.parse('a user sends the command "{text}"'))
def user_sends(
    ctx: Context,
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
    text: str,
) -> None:
    feed(run, dispatcher, bot, make_message_update(text, ctx.update_id))


@then("the bot should reply with the usage example")
def bot_asks_for_a_name(telegram: MockTelegram) -> None:
    sent = telegram.calls_to("sendMessage")
    assert sent, "expected a sendMessage call, got none"
    assert sent[-1].get("text", "") == locales.get("age", "en")


@then("the bot should reply with the guessed age and sample size")
def bot_replies_with_age(telegram: MockTelegram) -> None:
    sent = telegram.calls_to("sendMessage")
    assert sent, "expected a sendMessage call, got none"
    expected = locales.get("age_yes", "en", age=42, registered_times=12345)
    assert sent[-1].get("text", "") == expected, sent[-1]


@then("the bot should reply that it does not know")
def bot_replies_not_know(telegram: MockTelegram) -> None:
    sent = telegram.calls_to("sendMessage")
    assert sent, "expected a sendMessage call, got none"
    assert sent[-1].get("text", "") == locales.get("not_know", "en")


@then("the bot replies that fun functions are off")
def bot_says_fun_off(telegram: MockTelegram) -> None:
    sent = telegram.calls_to("sendMessage")
    assert sent, "expected a sendMessage call, got none"
    assert sent[-1].get("text", "") == locales.get("fun_off", "en")
