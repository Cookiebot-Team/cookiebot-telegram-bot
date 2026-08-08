"""Step definitions for x_owner_commands.

QA: `qa/features/x_owner_commands.feature` — authored, not ported. Contract:
`docs/contracts/x_owner_commands.md`.

Drives the real dispatcher against the mock Telegram API. Two things are
substituted: the gateway->worker queue (the broker is the outside world,
AGENTS.md §6) and `settings.owner_id`, which is read through
`cb_gateway.handlers.owner.get_settings` — the same `lru_cache` seam every
other suite uses for a non-default setting.

Needs a real database: `/grupos`, `/blacklist` and `/unblacklist` are reads
and writes against `groups` and `blacklist`, and a handler that invented an
answer with no store behind it would be worse than one that stayed quiet.
"""

from __future__ import annotations

from types import ModuleType
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from pytest_bdd import given, parsers, scenarios, then, when

from cb_core import jobs
from cb_core.settings import get_settings
from cb_gateway.handlers import owner as owner_handler
from qa.conftest import (
    GROUP_ID,
    USER_ID,
    Context,
    feed,
    make_private_message_update,
    next_update_id,
)
from qa.mock_telegram import MockTelegram

scenarios("x_owner_commands.feature")

OWNER_ID = 700100
SUBJECT_ID = 424243


@pytest.fixture(autouse=True)
def fake_queue(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def _fake_enqueue(job: str, *args: object, **kwargs: object) -> bool:
        calls.append((job, dict(kwargs)))
        return True

    monkeypatch.setattr(owner_handler, "enqueue", _fake_enqueue)
    return calls


@pytest.fixture(autouse=True)
def _clean_blacklist(database: ModuleType, run: Any) -> Any:
    from cb_core import db

    stmt = "DELETE FROM blacklist WHERE subject_id = ANY($1::bigint[])"
    run(db.execute(stmt, [SUBJECT_ID, 999111], name="qa_clean_blacklist"))
    yield
    run(db.execute(stmt, [SUBJECT_ID, 999111], name="qa_clean_blacklist"))


def _dm(telegram: MockTelegram, chat_id: int) -> list[dict[str, Any]]:
    return [
        call for call in telegram.calls_to("sendMessage") if int(call.get("chat_id", 0)) == chat_id
    ]


# ---------------------------------------------------------------------- given


@given("that the bot is running")
def bot_running(ctx: Context) -> None:
    ctx.bot_running = True


@given("that the sender is the bot owner")
def sender_is_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    patched = get_settings().model_copy(update={"owner_id": OWNER_ID})
    monkeypatch.setattr(owner_handler, "get_settings", lambda: patched)


@given("that the sender is not the bot owner")
def sender_is_not_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    patched = get_settings().model_copy(update={"owner_id": OWNER_ID})
    monkeypatch.setattr(owner_handler, "get_settings", lambda: patched)


@given(parsers.parse("the user {subject:d} is blacklisted"))
def user_is_blacklisted(run: Any, subject: int) -> None:
    from cb_core import ops

    run(ops.blacklist_add(subject, kind="user", reason="qa"))


# ----------------------------------------------------------------------- when


@when(parsers.parse('the owner sends "{text}" in a private chat'))
def owner_sends(run: Any, dispatcher: Dispatcher, bot: Bot, text: str) -> None:
    feed(
        run, dispatcher, bot, make_private_message_update(text, next_update_id(), user_id=OWNER_ID)
    )


@when(parsers.parse('that user sends "{text}" in a private chat'))
def other_sends(run: Any, dispatcher: Dispatcher, bot: Bot, text: str) -> None:
    feed(run, dispatcher, bot, make_private_message_update(text, next_update_id(), user_id=USER_ID))


# ----------------------------------------------------------------------- then


@then("the bot should list the groups and their total")
def lists_groups(telegram: MockTelegram) -> None:
    sent = _dm(telegram, OWNER_ID)
    assert sent, "the owner got no answer"
    body = sent[-1]["text"]
    assert "Total groups found:" in body
    # The QA group is seeded by qa/conftest.py's `database` fixture.
    assert str(GROUP_ID) in body


@then("the bot should say nothing")
def says_nothing(telegram: MockTelegram) -> None:
    """v1's owner branches are `elif ... and msg['from']['id'] == ownerID`, so
    a non-owner falls through to the generic "commands must be used in a group
    chat" branch — which `core_listcommand`'s port owns, not this one. What
    matters here is that this handler does not answer."""
    assert _dm(telegram, USER_ID) == []


@then("the bot should confirm the user was blacklisted")
def confirms_blacklist(telegram: MockTelegram, run: Any) -> None:
    assert _dm(telegram, OWNER_ID)[-1]["text"] == f"Blacklisted user with ID {SUBJECT_ID}"
    from cb_core import db

    row = run(
        db.fetchrow(
            "SELECT kind FROM blacklist WHERE subject_id = $1", SUBJECT_ID, name="qa_check_bl"
        )
    )
    assert row is not None and row["kind"] == "user"


@then("the bot should confirm the user was unblacklisted")
def confirms_unblacklist(telegram: MockTelegram, run: Any) -> None:
    assert _dm(telegram, OWNER_ID)[-1]["text"] == f"Unblacklisted user with ID {SUBJECT_ID}"
    from cb_core import db

    assert (
        run(
            db.fetchrow(
                "SELECT 1 FROM blacklist WHERE subject_id = $1", SUBJECT_ID, name="qa_check_bl"
            )
        )
        is None
    )


@then("the bot should say the user was not blacklisted")
def says_not_blacklisted(telegram: MockTelegram) -> None:
    assert _dm(telegram, OWNER_ID)[-1]["text"] == "User with ID 999111 was not blacklisted"


@then("the bot should queue the broadcast")
def queues_broadcast(telegram: MockTelegram, fake_queue: list[tuple[str, dict[str, Any]]]) -> None:
    assert len(fake_queue) == 1, fake_queue
    job, kwargs = fake_queue[0]
    assert job == jobs.BROADCAST_TO_GROUPS
    assert kwargs["text"] == "hello everyone"
    assert kwargs["owner_id"] == OWNER_ID
    assert _dm(telegram, OWNER_ID)[-1]["text"] == "Broadcast queued."


@then("the bot should explain how to use it")
def explains_usage(telegram: MockTelegram, fake_queue: list[tuple[str, dict[str, Any]]]) -> None:
    assert fake_queue == []
    assert _dm(telegram, OWNER_ID)[-1]["text"].startswith("Usage: /broadcast")


@then("the bot should explain that process control is the orchestrator's job")
def refuses_process_control(telegram: MockTelegram) -> None:
    assert _dm(telegram, OWNER_ID)[-1]["text"] == owner_handler.PROCESS_CONTROL_REFUSAL
