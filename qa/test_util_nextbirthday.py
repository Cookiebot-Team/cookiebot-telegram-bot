"""Step definitions for util_nextbirthday.

QA: qa/features/util_nextbirthday.feature (synced from Cookiebot-QA/features/util_nextbirthday.feature).
Contract: docs/contracts/util_nextbirthday.md.

`/nextbirthday` stays on the reply path (design R4) and is **not**
group-scoped (`cb_core.birthdays.all_users_with_birthday`, matching v1's own
`next_birthdays` exactly) — this suite drives it against a real database,
same as any other feature whose behaviour *is* the query
(`qa/test_fun_ship.py`'s identical framing for its own registry read).
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from pytest_bdd import given, parsers, scenarios, then, when

from cb_core import birthdays, db
from qa.conftest import Context, feed, make_message_update, next_update_id
from qa.mock_telegram import MockTelegram

scenarios("util_nextbirthday.feature")

# An id outside every other suite's seeded range.
_SEEDED_USER_ID = 768_200_001
_SEEDED_USERNAME = "upcoming_birthday_person"


@pytest.fixture(autouse=True)
def _clean_seeded_user(run: Callable[[Coroutine[Any, Any, Any]], Any]) -> Iterator[None]:
    yield
    try:
        db.pool()
    except RuntimeError:
        return
    run(
        db.execute(
            "DELETE FROM users WHERE user_id = $1", _SEEDED_USER_ID, name="qa_nextbday_clean"
        )
    )


class Ctx:
    def __init__(self) -> None:
        self.reply: str = ""

    def alloc_id(self) -> int:
        return next_update_id()


@pytest.fixture
def nextbday_ctx() -> Ctx:
    return Ctx()


@given("that the bot is in the group and properly set up")
def bot_set_up(ctx: Context) -> None:
    ctx.bot_running = True


@given("that the user is in the group")
def user_in_group() -> None:
    pass


@given("that a user has a birthday in 2 days")
def seed_upcoming_birthday(database: Any, run: Callable[[Coroutine[Any, Any, Any]], Any]) -> None:
    target = datetime.now(UTC).date() + timedelta(days=2)
    run(
        db.execute(
            """
            INSERT INTO users (user_id, username, birthdate)
            VALUES ($1, $2, $3)
            ON CONFLICT (user_id) DO UPDATE SET username = EXCLUDED.username, birthdate = EXCLUDED.birthdate
            """,
            _SEEDED_USER_ID,
            _SEEDED_USERNAME,
            target.replace(
                year=2000
            ),  # the year never matters -- only month/day are indexed/matched
            name="qa_nextbday_seed",
        )
    )


@when(parsers.parse('the user sends the command "{text}"'))
def user_sends(
    nextbday_ctx: Ctx,
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
    telegram: MockTelegram,
    text: str,
) -> None:
    feed(run, dispatcher, bot, make_message_update(text, nextbday_ctx.alloc_id()))
    sent = telegram.calls_to("sendMessage")
    assert sent, "expected a sendMessage call, got none"
    nextbday_ctx.reply = str(sent[-1].get("text", ""))


@then("the bot should reply with a list of the next users to have their birthdays, sorted by date")
def replies_with_the_list(nextbday_ctx: Ctx) -> None:
    body = nextbday_ctx.reply
    assert body.startswith(birthdays.bday_next_header("en"))
    for offset in range(1, 5):
        assert f"{offset} dias:\n" in body, body


@then("that user's name appears under the 2-day heading")
def named_person_appears_on_the_right_day(nextbday_ctx: Ctx) -> None:
    body = nextbday_ctx.reply
    day_2_section = body.split("2 dias:\n", 1)[1].split("\n\n", 1)[0]
    assert f"@{_SEEDED_USERNAME}" in day_2_section, body
