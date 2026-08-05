"""Step definitions for util_deletereposts.

QA: qa/features/util_deletereposts.feature (synced from
Cookiebot-QA/features/util_deletereposts.feature, with two corrections
documented in that file's header). Contract:
docs/contracts/util_deletereposts.md.

Drives the real dispatcher against the mock Telegram API and a real
`scheduled_posts` table: what this command deletes *is* those rows, so faking
the repository would only prove the handler can call a function (AGENTS.md §6).
The suite skips cleanly when no database is reachable.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from pytest_bdd import given, scenarios, then, when

from cb_core import db, locales, scheduled_posts
from qa.conftest import (
    ADMIN_ID,
    GROUP_ID,
    USER_ID,
    Context,
    feed,
    make_message_update,
    next_update_id,
)
from qa.mock_telegram import MockTelegram

scenarios("util_deletereposts.feature")

Run = Callable[[Coroutine[Any, Any, Any]], Any]

#: The chat the scheduled rows are attributed to. `/deleteposts` cancels by
#: *requester*, so the rows below claim `GROUP_ID` asked for them.
REQUESTER = GROUP_ID


@pytest.fixture(autouse=True)
def _clean_rows(database: Any, run: Run) -> Iterator[None]:
    """A real table, emptied of this group's rows around each scenario."""
    _wipe(run)
    yield
    _wipe(run)


def _wipe(run: Run) -> None:
    run(
        db.execute(
            "DELETE FROM scheduled_posts WHERE group_id = $1",
            GROUP_ID,
            name="qa_deletereposts_clean",
        )
    )


def _seed(run: Run, *, requester_chat_id: int = REQUESTER, origin_title: str = "FurShop") -> None:
    run(
        scheduled_posts.create(
            group_id=GROUP_ID,
            origin_title=origin_title,
            target_title="QA Group",
            days_remaining=3,
            next_run_at=datetime.now(UTC) + timedelta(days=1),
            source_chat_id=-100777,
            source_message_id=42,
            requester_chat_id=requester_chat_id,
            requester_message_id=7,
            requester_user_id=USER_ID,
        )
    )


# --------------------------------------------------------------------- given


@given("that the bot is in the group and properly set up")
def bot_set_up(ctx: Context) -> None:
    ctx.bot_running = True


@given("the user is an admin on that group")
def user_is_admin(telegram: MockTelegram) -> None:
    telegram.set_admins(GROUP_ID, [(USER_ID, "administrator")])


@given("that the user is in a group")
def user_is_a_plain_member(telegram: MockTelegram, run: Run) -> None:
    """Somebody else is the admin, so the sender is not. Starting from "nobody
    is an admin" would pass even if the handler forgot its check.

    Also seeds a row, so "no scheduled posts are deleted" has something it
    could have deleted. The QA scenario has no precondition step of its own and
    its wording must not change (AGENTS.md §6), so the seed rides along here.
    """
    telegram.set_admins(GROUP_ID, [(ADMIN_ID, "administrator")])
    _seed(run, origin_title="Survives The Refusal")


@given("the group has scheduled posts")
def group_has_scheduled_posts(run: Run) -> None:
    _seed(run, origin_title="Channel One")
    _seed(run, origin_title="Channel Two")
    assert run(scheduled_posts.count_for_group(GROUP_ID)) == 2


# ---------------------------------------------------------------------- when


@when("they use the /deletereposts command")
@when("they tried to use the /deletereposts command")
def use_the_command(run: Run, dispatcher: Dispatcher, bot: Bot) -> None:
    """QA spells it `/deletereposts`; v1 ships `/deleteposts`. Both resolve —
    the alias table is asserted in
    packages/cb-gateway/tests/test_deletereposts.py. The scenario sends QA's
    spelling, since that is what it says."""
    update_id = next_update_id()
    feed(run, dispatcher, bot, make_message_update("/deletereposts", update_id))


# ---------------------------------------------------------------------- then


@then("all scheduled posts requested by that group are deleted")
def rows_are_gone(run: Run) -> None:
    assert run(scheduled_posts.count_for_group(GROUP_ID)) == 0


@then("no scheduled posts are deleted")
def rows_are_untouched(run: Run) -> None:
    """The refusal must return *before* the delete, not merely reply after it."""
    assert run(scheduled_posts.count_for_group(GROUP_ID)) == 1
    assert run(scheduled_posts.find_by_origin_title("Survives The Refusal")) is not None


@then('the bot confirms with "Posts and reposts canceled!"')
def bot_confirms(telegram: MockTelegram) -> None:
    sent = telegram.calls_to("sendMessage")
    assert sent, "expected a confirmation"
    assert sent[-1]["text"] == locales.get("deletereposts_done", "en")


@then('the bot should send a message on the group saying "You are not a group admin!"')
def bot_refuses(telegram: MockTelegram) -> None:
    """v1's actual refusal (`Publisher.py:319-321`). QA asserts /configurar's
    wording plus a video; neither exists on this command — see the feature
    file's header."""
    sent = telegram.calls_to("sendMessage")
    assert sent, "expected a refusal"
    assert sent[-1]["text"] == locales.get("not_group_admin", "en")
    assert not telegram.calls_to("sendVideo"), "v1 sends no video for this command"
