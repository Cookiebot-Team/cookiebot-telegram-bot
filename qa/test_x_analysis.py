"""Step definitions for x_analysis.

QA: qa/features/x_analysis.feature (authored here — Cookiebot-QA has no
scenario for this feature; see the file's own header).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from pytest_bdd import given, parsers, scenarios, then, when

from cb_core import locales
from cb_gateway.handlers.analysis import render_payload
from qa.conftest import BOT_USERNAME, GROUP_ID, feed, make_message_update

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from aiogram import Bot, Dispatcher

    from qa.conftest import Context
    from qa.mock_telegram import MockTelegram

scenarios("x_analysis.feature")

#: The harness's own bot id (qa/conftest.py seeds updates with it).
BOT_ID = 424242

#: The message every "replying to a message" scenario replies to. `text` and
#: `message_id` are the two fields the assertions below name, and they are the
#: two a member reporting a problem would actually be asked for.
_REPLIED_TO: dict[str, Any] = {
    "message_id": 4242,
    "date": 0,
    "chat": {"id": GROUP_ID, "type": "supergroup", "title": "QA"},
    "from": {"id": BOT_ID, "is_bot": True, "first_name": "Cookiebot", "username": BOT_USERNAME},
    "text": "the message under analysis",
}


@given("that the bot is in the group and properly set up")
def bot_set_up(ctx: Context) -> None:
    ctx.bot_running = True


@given("that a user is in the group")
def user_in_group() -> None:
    pass


@when(parsers.parse('a user sends the command "{text}"'))
def user_sends(
    ctx: Context,
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
    text: str,
) -> None:
    feed(run, dispatcher, bot, make_message_update(text, ctx.update_id))


@when(parsers.parse('a user sends the command "{text}" replying to a message'))
def user_sends_replying(
    ctx: Context,
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
    text: str,
) -> None:
    feed(run, dispatcher, bot, make_message_update(text, ctx.update_id, reply_to=_REPLIED_TO))


@then("the bot should reply telling the user to reply to a message")
def bot_asks_for_a_reply(telegram: MockTelegram) -> None:
    sent = telegram.calls_to("sendMessage")
    assert sent, "expected a sendMessage call, got none"
    assert sent[-1].get("text", "") == locales.get("analyze", "en")


@then("the bot should reply with the replied-to message's fields")
def bot_dumps_the_payload(telegram: MockTelegram) -> None:
    sent = telegram.calls_to("sendMessage")
    assert sent, "expected a sendMessage call, got none"
    body = sent[-1].get("text", "")
    # v1's shape is `key: value` per line, and the two fields anyone reporting a
    # problem is asked for are the id and the text.
    assert "message_id: 4242" in body, body
    assert "the message under analysis" in body, body


@then("the bot reacts to the command with a thinking face")
def bot_reacts(telegram: MockTelegram) -> None:
    reactions = telegram.calls_to("setMessageReaction")
    assert reactions, f"expected a setMessageReaction call, got {telegram.calls}"
    # aiogram serialises the reaction list to JSON before it reaches the
    # transport, so the harness captures a string with the emoji escaped —
    # parse it back rather than matching against escaped code points.
    payload = reactions[-1].get("reaction", "[]")
    reaction = json.loads(payload) if isinstance(payload, str) else payload
    assert [r["emoji"] for r in reaction] == ["🤔"], reaction


@when("the payload to render is longer than a Telegram message allows")
def oversized_payload(ctx: Context) -> None:
    # One field whose value alone exceeds the whole budget — the shape a long
    # forwarded chain or a large `entities` array produces, which is what made
    # v1's own sendMessage fail with a 400 and answer nothing at all.
    ctx.rendered = render_payload({"text": "x" * 9000})  # type: ignore[attr-defined]


@then("the rendered dump fits in one message and says it was truncated")
def rendered_is_truncated(ctx: Context) -> None:
    rendered: str = ctx.rendered  # type: ignore[attr-defined]
    assert len(rendered) <= 4000, len(rendered)
    assert rendered.endswith("… (truncated)"), rendered[-40:]
