"""Step definitions for util_postforwarder.

QA: qa/features/util_postforwarder.feature — scenarios 1-2 synced from
Cookiebot-QA with the approval press and the scheduler tick made explicit
(see that file's header), scenarios 3-8 authored. Contract:
docs/contracts/util_postforwarder.md.

Drives the real dispatcher against the mock Telegram API and a real
`scheduled_posts` table. Three things are mocked, all of them the outside world
(AGENTS.md §6):

  * the arq broker — `enqueue` is replaced in the handler's own namespace, the
    seam `qa/test_util_everyone.py` established, and the recorded job is then
    run inline so the scenario can assert what it actually did;
  * the LLM router the caption translation goes through;
  * exchangerate-api, via `publisher_job.set_http_client`.

The publisher's own logic — the gate order, the fan-out's skip rules, the row
writes — is not mocked at any point.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Coroutine, Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from aiogram import Bot, Dispatcher
from pytest_bdd import given, parsers, scenarios, then, when

from cb_core import db, group_config, locales, pending_posts, scheduled_posts
from cb_core.settings import Settings, get_settings
from cb_gateway.handlers import publisher as publisher_handler
from cb_worker.jobs import publisher as publisher_job
from qa.conftest import (
    ADMIN_ID,
    GROUP_ID,
    USER_ID,
    Context,
    feed,
    make_callback_update,
    make_message_update,
    next_update_id,
)
from qa.mock_telegram import MockTelegram

scenarios("util_postforwarder.feature")

Run = Callable[[Coroutine[Any, Any, Any]], Any]

CHANNEL_ID = -1001900000002
CHANNEL_TITLE = "Group A Channel"
APPROVAL_CHAT_ID = -1001659344607
POSTMAIL_CHAT_ID = -1001869523792
#: The group b of the QA scenarios — a second target, distinct from GROUP_ID.
GROUP_B = GROUP_ID - 7


@pytest.fixture(autouse=True)
def _publisher_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """v1's hardcoded channels are settings in v2, and the feature is inert
    until they are set (design R5.5) — so every scenario has to configure them."""
    base = get_settings()
    configured = Settings(
        **{
            **base.model_dump(),
            "postmail_chat_id": POSTMAIL_CHAT_ID,
            "postmail_chat_link": "https://t.me/CookiebotPostmail",
            "approval_chat_id": APPROVAL_CHAT_ID,
            "exchangerate_api_key": "",
            "owner_id": 0,
        }
    )
    for module in (publisher_handler, publisher_job):
        monkeypatch.setattr(module, "get_settings", lambda: configured)


@pytest.fixture(autouse=True)
def fake_queue(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    """The arq broker, recorded rather than talked to."""
    calls: list[tuple[str, dict[str, Any]]] = []

    async def _fake_enqueue(job: str, *args: object, **kwargs: object) -> bool:
        calls.append((job, dict(kwargs)))  # type: ignore[arg-type]
        return True

    monkeypatch.setattr(publisher_handler, "enqueue", _fake_enqueue)
    return calls


@pytest.fixture(autouse=True)
def fake_translation(monkeypatch: pytest.MonkeyPatch) -> None:
    """The caption translator. Returns the input, which is also what the real
    one does on any failure (design R5.2) — the scenarios here assert routing
    and scheduling, not translation quality."""

    async def _identity(text: str, _target: str, *, group_id: int) -> str:
        return text

    monkeypatch.setattr(publisher_job, "_translate", _identity)


@pytest.fixture(autouse=True)
def _no_exchange_rates() -> Iterator[None]:
    publisher_job.set_http_client(
        httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(503)))
    )
    yield
    publisher_job.set_http_client(None)


@pytest.fixture(autouse=True)
def _real_pending_cache(valkey: Any) -> None:
    """A real Valkey, because the pending-post cache *is* the mechanism.

    v1 held the submitted post in a module-global dict (D-PF-3); v2 moved it to
    Valkey precisely because a replicated gateway cannot use process memory, and
    faking it here would assert the opposite of what the port is about
    (AGENTS.md §6). Skips cleanly when Valkey is unreachable, like every other
    suite that needs it.
    """


@pytest.fixture(autouse=True)
def _clean_scheduled_posts(database: Any, run: Run) -> Iterator[None]:
    """NOTE: never name a module-level autouse fixture `_reset_scenario_state`
    — see that fixture's docstring in qa/conftest.py."""
    _wipe(run)
    run(
        db.execute(
            """
            INSERT INTO groups (group_id, title, chat_type)
            VALUES ($1, 'Group B', 'supergroup') ON CONFLICT DO NOTHING
            """,
            GROUP_B,
            name="qa_pf_group_b",
        )
    )
    run(
        db.execute(
            "INSERT INTO group_configs (group_id) VALUES ($1) ON CONFLICT DO NOTHING",
            GROUP_B,
            name="qa_pf_config_b",
        )
    )
    yield
    _wipe(run)
    run(db.execute("DELETE FROM groups WHERE group_id = $1", GROUP_B, name="qa_pf_drop_b"))
    group_config._l1.clear()  # noqa: SLF001 - process-global


def _wipe(run: Run) -> None:
    run(
        db.execute(
            "DELETE FROM scheduled_posts WHERE group_id = ANY($1::bigint[])",
            [GROUP_ID, GROUP_B],
            name="qa_pf_clean",
        )
    )


def _channel_post(message_id: int, *, caption: str | None = "Ad copy here") -> dict[str, Any]:
    """A channel post forwarded into the group, as `reply_to_message`."""
    message: dict[str, Any] = {
        "message_id": message_id,
        "date": int(time.time()),
        "chat": {"id": GROUP_ID, "type": "supergroup", "title": "QA Group"},
        "from": {"id": 777000, "is_bot": False, "first_name": "Telegram"},
        "forward_from_chat": {
            "id": CHANNEL_ID,
            "type": "channel",
            "title": CHANNEL_TITLE,
            "username": "groupachannel",
        },
        "forward_from_message_id": message_id + 5000,
        "photo": [
            {
                "file_id": "ad-1",
                "file_unique_id": "uad",
                "width": 90,
                "height": 90,
                "file_size": 1,
            }
        ],
    }
    if caption is not None:
        message["caption"] = caption
    return message


def _plain_message(message_id: int) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "date": int(time.time()),
        "chat": {"id": GROUP_ID, "type": "supergroup", "title": "QA Group"},
        "from": {"id": USER_ID, "is_bot": False, "first_name": "Tester"},
        "text": "just a message",
    }


class Ctx:
    def __init__(self) -> None:
        self.forward_from_message_id: int | None = None
        self.bot: AsyncMock | None = None
        self.reply_text: str = ""


@pytest.fixture
def pf_ctx() -> Ctx:
    return Ctx()


def _submit(run: Run, dispatcher: Dispatcher, bot: Bot, pf_ctx: Ctx) -> None:
    """`/divulgar` in reply to a forwarded channel post."""
    update_id = next_update_id()
    replied = _channel_post(update_id - 1)
    pf_ctx.forward_from_message_id = replied["forward_from_message_id"]
    feed(
        run,
        dispatcher,
        bot,
        make_message_update("/divulgar", update_id, reply_to=replied),
    )


# --------------------------------------------------------------------- given


@given("that the bot is in the group and properly set up")
def bot_set_up(ctx: Context) -> None:
    ctx.bot_running = True


@given(
    "that the post forwarding feature is enabled on the group a "
    "and the getter feature is enabled on group b"
)
def group_b_accepts(run: Run) -> None:
    run(group_config.set_config(GROUP_B, publisher_post=True))
    group_config._l1.clear()  # noqa: SLF001


@given(
    "that the post forwarding feature is enabled on the group a "
    "and the getter feature is disabled on the group b"
)
def group_b_refuses(run: Run) -> None:
    run(group_config.set_config(GROUP_B, publisher_post=False))
    group_config._l1.clear()  # noqa: SLF001


@given("the user is an admin on that group")
def user_is_admin(telegram: MockTelegram) -> None:
    telegram.set_admins(GROUP_ID, [(USER_ID, "administrator")])


@given("that a post is waiting for approval")
def a_pending_post(run: Run, dispatcher: Dispatcher, bot: Bot, pf_ctx: Ctx) -> None:
    _submit(run, dispatcher, bot, pf_ctx)


# ---------------------------------------------------------------------- when


@when("a post is forwarded from group a to the bot")
def post_is_submitted(run: Run, dispatcher: Dispatcher, bot: Bot, pf_ctx: Ctx) -> None:
    _submit(run, dispatcher, bot, pf_ctx)


@when("the owner approves the post")
def owner_approves(
    run: Run,
    dispatcher: Dispatcher,
    bot: Bot,
    pf_ctx: Ctx,
    fake_queue: list[tuple[str, dict[str, Any]]],
    telegram: MockTelegram,
) -> None:
    """The 7-day, non-NSFW button, pressed *in the approval chat* — then the
    job it enqueues, run inline against a mock bot."""
    data = f"yPub {CHANNEL_ID} {GROUP_ID} {pf_ctx.forward_from_message_id} {USER_ID} 7 999 0"
    feed(
        run,
        dispatcher,
        bot,
        make_callback_update(data, next_update_id(), chat_id=APPROVAL_CHAT_ID),
    )
    assert fake_queue, "the approve press should have enqueued the render + fan-out"
    _job_name, kwargs = fake_queue[-1]
    pf_ctx.bot = AsyncMock()
    pf_ctx.bot.get_chat.return_value = SimpleNamespace(
        title=CHANNEL_TITLE, username="groupachannel", is_forum=False
    )
    pf_ctx.bot.get_chat_member.return_value = SimpleNamespace(
        user=SimpleNamespace(first_name="Ana", username="ana")
    )
    pf_ctx.bot.send_photo.return_value = SimpleNamespace(message_id=4242)
    run(publisher_job._run_approve(pf_ctx.bot, **kwargs))  # noqa: SLF001


@when("a day passes and the delivery sweep runs")
def sweep_runs(run: Run, pf_ctx: Ctx) -> None:
    """The scenario has to wait a day, because v1 makes it.

    `create_job` adds `timedelta(days=1)` unconditionally (`Publisher.py:96`),
    so a post approved now is first forwarded *tomorrow* — never in the tick
    that follows the approval. The QA scenario reads as though approval and
    delivery are one step; they are two, a day apart. Backdating the rows is
    how that day passes here, and `test_next_run_is_always_tomorrow` in
    packages/cb-worker/tests asserts the gap itself.
    """
    run(
        db.execute(
            "UPDATE scheduled_posts SET next_run_at = now() - interval '1 minute' "
            "WHERE group_id = ANY($1::bigint[])",
            [GROUP_ID, GROUP_B],
            name="qa_pf_advance_a_day",
        )
    )
    pf_ctx.bot = AsyncMock()
    pf_ctx.bot.get_chat.return_value = SimpleNamespace(is_forum=False)
    run(publisher_job._run_delivery(pf_ctx.bot))  # noqa: SLF001


@when("the user sends /divulgar without replying to a message")
def submit_without_reply(run: Run, dispatcher: Dispatcher, bot: Bot) -> None:
    feed(run, dispatcher, bot, make_message_update("/divulgar", next_update_id()))


@when("the user replies /divulgar to an ordinary group message")
def submit_on_a_plain_message(run: Run, dispatcher: Dispatcher, bot: Bot) -> None:
    update_id = next_update_id()
    feed(
        run,
        dispatcher,
        bot,
        make_message_update("/divulgar", update_id, reply_to=_plain_message(update_id - 1)),
    )


@when("the user replies /divulgar to a channel post with no caption")
def submit_without_caption(run: Run, dispatcher: Dispatcher, bot: Bot) -> None:
    update_id = next_update_id()
    feed(
        run,
        dispatcher,
        bot,
        make_message_update(
            "/divulgar", update_id, reply_to=_channel_post(update_id - 1, caption=None)
        ),
    )


@when("someone presses approve from an ordinary group")
def approve_from_the_wrong_chat(run: Run, dispatcher: Dispatcher, bot: Bot, pf_ctx: Ctx) -> None:
    """D-PF-2. v1 checked nothing at all (`COOKIEBOT.py:372-373`) and relied on
    the buttons only appearing in a private chat — but a callback payload is a
    plain string anyone can replay once they know its shape."""
    data = f"yPub {CHANNEL_ID} {GROUP_ID} {pf_ctx.forward_from_message_id} {USER_ID} 7 999 0"
    feed(run, dispatcher, bot, make_callback_update(data, next_update_id(), chat_id=GROUP_ID))


@when(parsers.parse("they reply /repost {days:d} to a message"))
def admin_reposts(run: Run, dispatcher: Dispatcher, bot: Bot, days: int) -> None:
    update_id = next_update_id()
    feed(
        run,
        dispatcher,
        bot,
        make_message_update(f"/repost {days}", update_id, reply_to=_plain_message(update_id - 1)),
    )


@when("a plain member replies /repost to a message")
def member_reposts(run: Run, dispatcher: Dispatcher, bot: Bot, telegram: MockTelegram) -> None:
    telegram.set_admins(GROUP_ID, [(ADMIN_ID, "administrator")])
    update_id = next_update_id()
    feed(
        run,
        dispatcher,
        bot,
        make_message_update("/repost", update_id, reply_to=_plain_message(update_id - 1)),
    )


# ---------------------------------------------------------------------- then


@then(
    "the group b should receive the forwarded post with the original source "
    "and any relevant information about it"
)
def group_b_received_it(pf_ctx: Ctx) -> None:
    assert pf_ctx.bot is not None
    calls = pf_ctx.bot.forward_message.await_args_list
    targets = [c.args[0] for c in calls]
    assert GROUP_B in targets, f"expected a forward into group b, got {targets}"
    forwarded = next(c for c in calls if c.args[0] == GROUP_B)
    # A forward, not a re-send: Telegram's own attribution header *is* "the
    # original source" the spec asks for (`Publisher.py:347-351`).
    assert forwarded.args[1] == POSTMAIL_CHAT_ID


@then("the bot should not forward the post to the group b")
def group_b_received_nothing(pf_ctx: Ctx) -> None:
    assert pf_ctx.bot is not None
    targets = [c.args[0] for c in pf_ctx.bot.forward_message.await_args_list]
    assert GROUP_B not in targets, f"group b opted out but was sent {targets}"


@then(parsers.parse('the bot answers "{expected}"'))
@then(parsers.parse('the bot confirms with "{expected}"'))
def bot_answers(telegram: MockTelegram, expected: str) -> None:
    sent = [c for c in telegram.calls_to("sendMessage") if int(c["chat_id"]) == GROUP_ID]
    assert sent, "expected a reply in the group"
    assert sent[-1]["text"] == expected


@then("no post is rendered or scheduled")
def nothing_happened(fake_queue: list[tuple[str, dict[str, Any]]], run: Run) -> None:
    assert fake_queue == [], "an unauthorised press must not enqueue the render"
    assert run(scheduled_posts.count_for_group(GROUP_ID)) == 0


@then("the group has one scheduled post")
def one_row(run: Run) -> None:
    assert run(scheduled_posts.count_for_group(GROUP_ID)) == 1


@then("the group has no scheduled posts")
def no_rows(run: Run) -> None:
    assert run(scheduled_posts.count_for_group(GROUP_ID)) == 0


@then("the pending post is gone")
def pending_cleared(run: Run, pf_ctx: Ctx) -> None:
    assert run(pending_posts.get(pf_ctx.forward_from_message_id or 0)) is None


__all__ = ["locales"]
