"""Step definitions for util_postgetter.

QA: qa/features/util_postgetter.feature — scenario 1 synced verbatim from
Cookiebot-QA, scenarios 2-4 authored (the spec has nothing for `publisher_ask`
or the `publisher_post` delivery gate). Contract:
docs/contracts/util_postgetter.md.

Drives the real dispatcher against the mock Telegram API. The delivery
scenarios run `cb_worker.jobs.publisher`'s sweep inline against the same mock
bot — the cron is the only code that can perform the `publisher_post` re-check,
and mocking it would assert nothing.

`make_message_update` has no shape for a Telegram auto-forward, so
`_auto_forward` below builds one: `sender_chat` set to the linked channel,
`forward_from_chat`/`forward_from_message_id` set, a caption, and the sender's
`first_name` as the literal `"Telegram"` that v1 discriminates on
(`COOKIEBOT.py:165`).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Coroutine, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher
from pytest_bdd import given, scenarios, then, when

from cb_core import db, group_config, locales, scheduled_posts
from cb_worker.jobs import publisher as publisher_job
from qa.conftest import GROUP_ID, USER_ID, Context, feed, next_update_id
from qa.mock_telegram import MockTelegram

scenarios("util_postgetter.feature")

Run = Callable[[Coroutine[Any, Any, Any]], Any]

CHANNEL_ID = -1001900000001
CHANNEL_TITLE = "FurShop Channel"
POSTMAIL_CHAT_ID = -1001869523792


@pytest.fixture(autouse=True)
def _clean_scheduled(database: Any, run: Run) -> Iterator[None]:
    _wipe(run)
    yield
    _wipe(run)
    run(group_config.set_config(GROUP_ID, publisher_ask=True, publisher_post=False))
    group_config._l1.clear()  # noqa: SLF001 - process-global, same reset the other suites do


def _wipe(run: Run) -> None:
    run(db.execute("DELETE FROM scheduled_posts WHERE group_id = $1", GROUP_ID, name="qa_pg_clean"))


def _auto_forward(update_id: int) -> dict[str, Any]:
    """What Telegram sends when a linked channel's post lands in the group."""
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": int(time.time()),
            "chat": {"id": GROUP_ID, "type": "supergroup", "title": "QA Group"},
            # v1's discriminator: Telegram itself is the apparent sender.
            "from": {"id": 777000, "is_bot": False, "first_name": "Telegram"},
            "sender_chat": {"id": CHANNEL_ID, "type": "channel", "title": CHANNEL_TITLE},
            "forward_from_chat": {
                "id": CHANNEL_ID,
                "type": "channel",
                "title": CHANNEL_TITLE,
                "username": "furshopchannel",
            },
            "forward_from_message_id": update_id + 5000,
            "caption": "Commissions open! https://furshop.example/store",
            "photo": [
                {
                    "file_id": "ad-photo-1",
                    "file_unique_id": "uad1",
                    "width": 90,
                    "height": 90,
                    "file_size": 1,
                }
            ],
        },
    }


class Ctx:
    def __init__(self) -> None:
        self.post_id: Any = None


@pytest.fixture
def pg_ctx() -> Ctx:
    return Ctx()


# --------------------------------------------------------------------- given


@given("that the bot is in the group and properly set up")
def bot_set_up(ctx: Context) -> None:
    ctx.bot_running = True


@given("that the post forwarding feature is enabled on the group")
def receiving_enabled(run: Run) -> None:
    run(group_config.set_config(GROUP_ID, publisher_post=True))
    group_config._l1.clear()  # noqa: SLF001


@given("the post is forwarded from another group or channel")
def a_post_is_scheduled_for_this_group(run: Run, pg_ctx: Ctx) -> None:
    """A campaign from another channel, already approved and queued for here."""
    pg_ctx.post_id = run(
        scheduled_posts.create(
            group_id=GROUP_ID,
            origin_title=CHANNEL_TITLE,
            target_title="QA Group",
            days_remaining=3,
            next_run_at=datetime.now(UTC) - timedelta(minutes=1),
            source_chat_id=POSTMAIL_CHAT_ID,
            source_message_id=4242,
            requester_chat_id=-1002,
            requester_message_id=7,
            requester_user_id=USER_ID,
        )
    )


@given("that the group has turned the sharing offer off")
def sharing_offer_off(run: Run) -> None:
    run(group_config.set_config(GROUP_ID, publisher_ask=False))
    group_config._l1.clear()  # noqa: SLF001


@given("that the group has a scheduled post due")
def a_due_post(run: Run, pg_ctx: Ctx) -> None:
    a_post_is_scheduled_for_this_group(run, pg_ctx)


@given("that the group has turned off receiving posts")
def receiving_disabled(run: Run) -> None:
    run(group_config.set_config(GROUP_ID, publisher_post=False))
    group_config._l1.clear()  # noqa: SLF001


# ---------------------------------------------------------------------- when


@when("the user views the post")
@when("the delivery sweep runs")
def run_the_sweep(run: Run, pg_ctx: Ctx) -> None:
    """v1's `scheduler_pull` (`Publisher.py:329-357`), as v2's cron body.

    A real `Bot` is not needed: what this scenario asserts is which Telegram
    call the sweep makes with which arguments, and the sweep's own decisions
    (the consent re-check, the row's fate) run against the real table.
    """
    pg_ctx.bot = AsyncMock()  # type: ignore[attr-defined]
    run(publisher_job._run_delivery(pg_ctx.bot))  # type: ignore[attr-defined] # noqa: SLF001


@when("Telegram auto-forwards a linked channel's ad into the group")
def telegram_auto_forwards(run: Run, dispatcher: Dispatcher, bot: Bot) -> None:
    feed(run, dispatcher, bot, _auto_forward(next_update_id()))


# ---------------------------------------------------------------------- then


@then("they should see the original source of the post and any relevant information about it")
def the_post_is_forwarded_with_its_attribution(pg_ctx: Ctx) -> None:
    """`forwardMessage`, not a re-send: Telegram's own "Forwarded from" header
    is what the spec calls "the original source", and the inline keyboard
    `prepare_post` attached in the Mural rides along with a forward
    (`Publisher.py:347-351`)."""
    bot: AsyncMock = pg_ctx.bot  # type: ignore[attr-defined]
    bot.forward_message.assert_awaited_once()
    args = bot.forward_message.await_args
    assert args.args[0] == GROUP_ID
    assert args.args[1] == POSTMAIL_CHAT_ID
    assert args.args[2] == 4242
    # D-PG-1: no configured topic means no argument, not v1's "9999" sentinel.
    assert args.kwargs["message_thread_id"] is None


@then("the bot offers to share it")
def the_offer_is_made(telegram: MockTelegram) -> None:
    sent = telegram.calls_to("sendMessage")
    assert sent, "expected the share offer"
    assert sent[-1]["text"] == locales.get("publisher_ask_prompt", "en")


@then("the offer carries an accept and a decline button")
def the_offer_has_both_buttons(telegram: MockTelegram) -> None:
    import json

    markup = telegram.calls_to("sendMessage")[-1]["reply_markup"]
    keyboard = (
        json.loads(markup)["inline_keyboard"]
        if isinstance(markup, str)
        else markup["inline_keyboard"]
    )
    assert [row[0]["text"] for row in keyboard] == ["✔️", "❌"]
    assert keyboard[0][0]["callback_data"].startswith("SendToApprovalPub ")
    # v1's group prompt sends a bare `nPub` with no message id (`:52`).
    assert keyboard[1][0]["callback_data"] == "nPub"


@then("the bot says nothing")
def nothing_is_said(telegram: MockTelegram) -> None:
    assert not telegram.calls_to("sendMessage")


@then("nothing is forwarded into the group")
def nothing_forwarded(pg_ctx: Ctx) -> None:
    bot: AsyncMock = pg_ctx.bot  # type: ignore[attr-defined]
    bot.forward_message.assert_not_awaited()


@then("the scheduled post is dropped")
def the_row_is_gone(run: Run) -> None:
    """D-PG-4, preserved: v1 deletes rather than pauses (`:342-345`), so a
    group that withdraws consent drains its backlog permanently."""
    assert run(scheduled_posts.count_for_group(GROUP_ID)) == 0
