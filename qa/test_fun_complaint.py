"""Step definitions for fun_complaint.

QA: qa/features/fun_complaint.feature (synced from
Cookiebot-QA/features/fun_complaint.feature, wording of its two scenarios
unchanged, plus two net-new scenarios for the fun-off gate and for a reply to
a caption that carries neither Milton signature — see that file's own header
comment). Contract: docs/contracts/fun_complaint.md (once T6 lands).

Drives the real dispatcher (`cb_gateway.main.dp`, via `qa/conftest.py`'s
`dispatcher` fixture) against the mock Telegram API. The feature is entirely
stateless (spec: "Persistence: none") so no database is needed for the happy
path — only the fun-off scenario touches `group_config`, and it needs the real
`database` fixture to flip `functions_fun`, the same pattern
`qa/test_fun_ship.py`'s `fun_disabled` step and `qa/test_fun_firecracker.py`'s
`fun_disabled` step use for their own gates (AGENTS.md §6 forbids mocking our
own code in an acceptance test).

Entry 2's photo prompt is fabricated the same way `qa/test_core_rules.py` and
`qa/test_core_welcome.py` fabricate their own reply targets: the mock records
the outgoing request, not the shape Telegram would hand back, so the prompt
handed to the next `reply_to_message` is built by hand from what was actually
sent. The one structural difference here is that entry 2 matches on a photo's
*caption*, never on `.text` (D-CP-3), so the fabricated prompt carries a
`caption` field, not a `text` one.

The 10-20s hold (`_schedule_tail`/`_delayed_reveal`,
`cb_gateway/handlers/complaint.py`) is never awaited for real: `_zero_hold_delay`
monkeypatches the handler's module-level `_sleep` to an instant no-op (design
R3.4), and `bot_answers_after_hold` below explicitly drives the scheduled
`asyncio.create_task` to completion via the shared session loop instead of
sleeping or polling for it.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable, Coroutine, Iterator
from typing import TYPE_CHECKING, Any

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from cb_core import group_config, locales
from qa.conftest import GROUP_ID, Context, feed, make_message_update, next_update_id

if TYPE_CHECKING:
    from aiogram import Bot, Dispatcher

    from qa.mock_telegram import MockTelegram

scenarios("fun_complaint.feature")

_PROTOCOL_CAPTION_RE = re.compile(r"^Protocol: \d{2}-\d{6}/\d{4}$")

_BOT_FROM = {
    "id": 424242,
    "is_bot": True,
    "first_name": "Cookiebot",
    "username": "CookieMWbot",
}


@pytest.fixture(autouse=True)
def _zero_hold_delay(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Design R3.4: the tail's `asyncio.sleep` is a module attribute so a test
    can replace it instead of waiting out v1's real 10-20s hold
    (`Miscellaneous.py:256`, D-CP-4)."""
    from cb_gateway.handlers import complaint as complaint_handler

    async def _instant_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(complaint_handler, "_sleep", _instant_sleep)
    yield


@pytest.fixture(autouse=True)
def _reset_fun_gate(run: Callable[[Coroutine[Any, Any, Any]], Any]) -> Iterator[None]:
    """`group_config._l1` is process-global; the "fun off" scenario flips
    `functions_fun` on the shared `GROUP_ID` and must not leak into a later
    scenario (same fix `qa/test_fun_ship.py` and `qa/test_fun_firecracker.py`
    apply for their own gates)."""
    from cb_core import db

    yield
    try:
        db.pool()
    except RuntimeError:
        return  # no database in this run; nothing was ever persisted either
    run(group_config.set_config(GROUP_ID, functions_fun=True))
    group_config._l1.clear()  # noqa: SLF001 - the L1 dict is the seam the harness owns


class Ctx:
    """Per-scenario state on top of qa/conftest.py's base `Context`."""

    def __init__(self) -> None:
        self.milton_prompt: dict[str, Any] | None = None
        self.unrelated_prompt: dict[str, Any] | None = None
        self.pending_before: set[asyncio.Task[None]] = set()

    def alloc_id(self) -> int:
        # Shared process-wide counter: a per-scenario counter collides with
        # earlier scenarios and the dedupe middleware drops the update.
        return next_update_id()


@pytest.fixture
def complaint_ctx() -> Ctx:
    return Ctx()


def _reply_to_milton_prompt(complaint_ctx: Ctx) -> dict[str, Any]:
    assert complaint_ctx.milton_prompt is not None, "no /complaint prompt was sent yet"
    return complaint_ctx.milton_prompt


# --------------------------------------------------------------------- given


@given("that the bot is in the group and properly set up")
def bot_set_up(ctx: Context) -> None:
    ctx.bot_running = True


@given("that the user is a member of the group")
def user_in_group() -> None:
    pass


@given("the fun feature is turned off")
def fun_disabled(database: Any, run: Callable[[Coroutine[Any, Any, Any]], Any]) -> None:
    run(group_config.set_config(GROUP_ID, functions_fun=False))
    group_config._l1.clear()  # noqa: SLF001


@given("that the user has received the fun complaint message")
def user_has_received_complaint_message(
    complaint_ctx: Ctx,
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
    telegram: MockTelegram,
) -> None:
    update_id = complaint_ctx.alloc_id()
    feed(run, dispatcher, bot, make_message_update("/complaint", update_id))
    sent = telegram.calls_to("sendPhoto")
    assert sent, "expected a sendPhoto call, got none"
    # The mock records the raw request payload, not the response Telegram would
    # hand back, so the prompt's own message id is fabricated (same idiom as
    # `qa/test_core_rules.py`'s `new_rules_prompt` / `qa/test_core_welcome.py`'s
    # `new_welcome_prompt`) — the handler only ever compares
    # `reply_to_message.caption`, never its id. Unlike those two, this is a
    # photo caption, not text (D-CP-3), so `caption` is what's carried here.
    complaint_ctx.milton_prompt = {
        "message_id": update_id,
        "date": int(time.time()),
        "chat": {"id": GROUP_ID, "type": "supergroup", "title": "QA Group"},
        "from": _BOT_FROM,
        "caption": str(sent[-1].get("caption", "")),
        "photo": [
            {
                "file_id": "milton-photo",
                "file_unique_id": "u-milton-photo",
                "width": 90,
                "height": 90,
                "file_size": 1,
            }
        ],
    }


@given("the user has received a photo with an unrelated caption")
def user_has_received_unrelated_photo(complaint_ctx: Ctx) -> None:
    """A photo whose caption carries neither `MILTON_SIGNATURES` string
    (`cb_gateway/handlers/complaint.py`) — entry 2 must not arm for it. Not
    sent through the bot: v1's `_is_milton_reply` equivalent reads only the
    replied-to caption, and reproduces regardless of who sent the photo
    (D-CP-3: "including one sent by a different bot")."""
    complaint_ctx.unrelated_prompt = {
        "message_id": complaint_ctx.alloc_id(),
        "date": int(time.time()),
        "chat": {"id": GROUP_ID, "type": "supergroup", "title": "QA Group"},
        "from": _BOT_FROM,
        "caption": "Just a regular photo, nothing to see here.",
        "photo": [
            {
                "file_id": "unrelated-photo",
                "file_unique_id": "u-unrelated-photo",
                "width": 90,
                "height": 90,
                "file_size": 1,
            }
        ],
    }


# ---------------------------------------------------------------------- when


@when(parsers.parse('the user types the command "{command}"'))
def user_types_command(
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
    complaint_ctx: Ctx,
    command: str,
) -> None:
    feed(run, dispatcher, bot, make_message_update(command, complaint_ctx.alloc_id()))


@when("the user responds to the message with their own complaint")
def user_responds_with_complaint(
    complaint_ctx: Ctx,
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
) -> None:
    from cb_gateway.handlers import complaint as complaint_handler

    # Snapshot before feeding: `_schedule_tail` adds the new tail's task to
    # this module-level set synchronously, before `complaint_answer` returns
    # (design R3.2), so the diff after `feed()` isolates the task this
    # scenario's own reply scheduled.
    complaint_ctx.pending_before = set(complaint_handler._pending_tails)  # noqa: SLF001
    feed(
        run,
        dispatcher,
        bot,
        make_message_update(
            "This is my complaint.",
            complaint_ctx.alloc_id(),
            reply_to=_reply_to_milton_prompt(complaint_ctx),
        ),
    )


@when("the user responds to that photo with their own complaint")
def user_responds_to_unrelated_photo(
    complaint_ctx: Ctx,
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
) -> None:
    assert complaint_ctx.unrelated_prompt is not None, "no unrelated photo was set up yet"
    feed(
        run,
        dispatcher,
        bot,
        make_message_update(
            "This is my complaint.",
            complaint_ctx.alloc_id(),
            reply_to=complaint_ctx.unrelated_prompt,
        ),
    )


# ---------------------------------------------------------------------- then


@then("the bot should send a fun complaint message to the group")
def bot_sends_complaint_message(telegram: MockTelegram) -> None:
    """QA/v1 conflict #1 (spec.md): v1 sends **one** message — a photo whose
    caption is the invitation — not a separate text message plus a picture.
    This step and the sibling one below both look at that same single
    `sendPhoto` call, from the "message" half."""
    sent = telegram.calls_to("sendPhoto")
    assert sent, "expected a sendPhoto call, got none"
    # qa/conftest.py's `_user()` names every mock sender "Tester".
    assert str(sent[-1].get("caption", "")) == locales.get("complaint", "en", user="Tester"), sent[
        -1
    ]


@then("the bot should send a fun complaint picture to the group")
def bot_sends_complaint_picture(telegram: MockTelegram) -> None:
    sent = telegram.calls_to("sendPhoto")
    assert sent, "expected a sendPhoto call, got none"
    assert int(sent[-1].get("chat_id", 0)) == GROUP_ID


@then("prompt the user to answer the message with a complaint of their own")
def bot_prompts_for_reply() -> None:
    """The photo itself *is* the prompt (design R4.1-R4.2): its caption
    invites a reply. Nothing further goes out beyond the single `sendPhoto`
    call the two steps above already checked."""


@then("the bot should send a voice message with a on-hold music to the group")
def bot_sends_hold_voice(complaint_ctx: Ctx, telegram: MockTelegram) -> None:
    prompt = _reply_to_milton_prompt(complaint_ctx)
    deleted = telegram.calls_to("deleteMessage")
    assert any(int(call.get("message_id", -1)) == prompt["message_id"] for call in deleted), (
        prompt,
        deleted,
    )
    sent = telegram.calls_to("sendVoice")
    assert sent, "expected a sendVoice call, got none"
    caption = str(sent[-1].get("caption", ""))
    assert _PROTOCOL_CAPTION_RE.match(caption), caption


@then("then after some minutes answer with a random phrase.")
def bot_answers_after_hold(
    complaint_ctx: Ctx,
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    telegram: MockTelegram,
) -> None:
    from cb_gateway.handlers import complaint as complaint_handler

    # Drive the scheduled tail to completion instead of sleeping or polling
    # for it — `_zero_hold_delay` above only makes the sleep instant, the task
    # still needs the loop's cooperation to actually run. If it already ran to
    # completion during `feed()` above, it has already removed itself from
    # `_pending_tails` (design R3.2's `add_done_callback`) and the diff is
    # empty — nothing left to await, and the effects already landed.
    tasks = complaint_handler._pending_tails - complaint_ctx.pending_before  # noqa: SLF001
    if tasks:

        async def _await_tail_tasks() -> None:
            await asyncio.gather(*tasks)

        run(_await_tail_tasks())

    # Both the Milton photo and the hold voice note get deleted over the full
    # cycle (spec: "Side effects"); the photo deletion was already asserted by
    # the previous Then step, so seeing two by now confirms the voice note's
    # own deletion landed too.
    deleted = telegram.calls_to("deleteMessage")
    assert len(deleted) == 2, deleted

    answers = set(locales.lines("answers", "en"))
    sent_messages = telegram.calls_to("sendMessage")
    assert sent_messages, "expected a sendMessage call, got none"
    assert str(sent_messages[-1].get("text", "")) in answers, sent_messages[-1]


@then("the bot should reply with a message saying that the fun feature is turned off")
def bot_says_fun_off(telegram: MockTelegram) -> None:
    sent = telegram.calls_to("sendMessage")
    assert sent, "expected a sendMessage call, got none"
    assert len(sent) == 1, sent  # gated off: one reply, nothing else
    assert str(sent[-1].get("text", "")) == locales.get("fun_off", "en")


@then("the bot sends nothing else")
def bot_sends_nothing_else(telegram: MockTelegram) -> None:
    assert not telegram.calls_to("sendPhoto")
    assert not telegram.calls_to("sendVoice")
    assert not telegram.calls_to("deleteMessage")


@then("the user receives no response")
def no_response(telegram: MockTelegram) -> None:
    assert not telegram.calls_to("sendMessage")
    assert not telegram.calls_to("sendPhoto")
    assert not telegram.calls_to("sendVoice")
    assert not telegram.calls_to("deleteMessage")
