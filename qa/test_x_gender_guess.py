"""Step definitions for x_gender_guess.

QA: qa/features/x_gender_guess.feature (authored here — Cookiebot-QA has no
scenario for this feature; see the file's own header).

genderize.io is stubbed via `gender.set_http_client` with an
`httpx.MockTransport`, the same boundary-stubbing idiom
`qa/test_util_doomlist.py` uses for cas.chat and burrbot.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from cb_core import group_config, locales
from cb_gateway.handlers import gender
from qa.conftest import GROUP_ID, feed, make_message_update

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Iterator

    from aiogram import Bot, Dispatcher

    from qa.conftest import Context
    from qa.mock_telegram import MockTelegram

scenarios("x_gender_guess.feature")


def _stub_transport(
    *, gender_value: str | None, probability: float | None, count: int, down: bool = False
) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if down:
            raise httpx.ConnectError("simulated genderize.io outage", request=request)
        return httpx.Response(
            200,
            json={"name": "x", "gender": gender_value, "probability": probability, "count": count},
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def _reset_gender_state() -> Iterator[None]:
    """Same reasoning as `qa/test_x_age_guess.py`'s own reset fixture."""
    gender._breaker = gender.Breaker()  # noqa: SLF001
    yield
    gender.set_http_client(None)


@given("that the bot is in the group and properly set up")
def bot_set_up(ctx: Context) -> None:
    ctx.bot_running = True


@given("that a user is in the group")
def user_in_group() -> None:
    pass


@given("genderize.io is stubbed to answer normally")
def genderize_default() -> None:
    gender.set_http_client(_stub_transport(gender_value="male", probability=0.9, count=500))


@given(
    parsers.parse(
        'that genderize.io reports "{value}" with {pct:d}% probability from {count:d} records'
    )
)
def genderize_reports(value: str, pct: int, count: int) -> None:
    gender.set_http_client(_stub_transport(gender_value=value, probability=pct / 100, count=count))


@given("that genderize.io reports zero records")
def genderize_zero() -> None:
    gender.set_http_client(_stub_transport(gender_value=None, probability=None, count=0))


@given("that genderize.io is down")
def genderize_down() -> None:
    gender.set_http_client(_stub_transport(gender_value=None, probability=None, count=0, down=True))


@given(parsers.parse("that genderize.io reports a null gender from {count:d} records"))
def genderize_null_gender(count: int) -> None:
    gender.set_http_client(_stub_transport(gender_value=None, probability=None, count=count))


@given("that fun functions are disabled for the group")
def fun_disabled(database: Any, run: Callable[[Coroutine[Any, Any, Any]], Any]) -> None:
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


@then("the bot should reply with the gender usage example")
def bot_asks_for_a_name(telegram: MockTelegram) -> None:
    sent = telegram.calls_to("sendMessage")
    assert sent, "expected a sendMessage call, got none"
    assert sent[-1].get("text", "") == locales.get("gender_exemple", "en")


@then("the bot should reply with the guessed gender and probability")
def bot_replies_with_gender(telegram: MockTelegram) -> None:
    sent = telegram.calls_to("sendMessage")
    assert sent, "expected a sendMessage call, got none"
    expected = locales.get_nested("gender", "male", "en", probability=90, registered_times=500)
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


@then("the bot should reply with the unknown-gender text")
def bot_replies_unknown_gender(telegram: MockTelegram) -> None:
    sent = telegram.calls_to("sendMessage")
    assert sent, "expected a sendMessage call, got none"
    expected = locales.get_nested(
        "gender", "unknown", "en", probability_str="?", registered_times=3
    )
    assert sent[-1].get("text", "") == expected, sent[-1]
