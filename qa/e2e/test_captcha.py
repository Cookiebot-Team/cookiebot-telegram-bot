"""The captcha, driven the way a person drives it: a newcomer joins, the bot
challenges them, and the *right button* lets them through.

This is the one scenario in the suite that could not run at all until
`tg_sandbox.control_api.join_chat` started storing the join's own service
message. `groupguardian._issue_challenge` answers a join with
`message.reply(...)`, which sends `reply_to_message_id` pointing at that
message, and `telegram_api._require_message` looks the id up in the store —
so with the join queued but never stored, every issuance came back
`400 Bad Request: message to reply not found` and the feature looked broken
while it was fine. That failure mode is exactly what an e2e suite is for, and
this file is the regression: it asserts the challenge is *replied to the join
message*, not merely sent.

`captcha_group_id`, not `group_id`: captcha is the join chain's first link,
so every other join scenario in this suite deliberately runs with the gate
closed (see `qa/e2e/conftest.py`).

Both tests run once per language too (`captcha_group_id` is parametrized on
`qa/e2e/conftest.py`'s `lang` fixture). Most assertions here are unaffected by
language on purpose: every button is matched on `callback_data`'s option half
(`_APPROVE = "APPROVE"`), which is wire shape, not display text, and is
identical in every language — v1's actual behaviour, not a gap in this suite.
The one exception is the approve button's *label*, which genuinely is
localised (`groupguardian._captcha_strings`'s nested `"captcha"` catalog
object, `button_approve`); the first test below checks it against the real
catalog value for the group's own language, which is the one assertion in
this file a broken or missing `pt` captcha catalog entry would actually fail.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from cb_core import locales
from qa.e2e.client import (
    SandboxClient,
    calls_to,
    describe_recent_calls,
    messages_in,
    wait_for,
)

pytestmark = pytest.mark.e2e

#: `groupguardian._APPROVE_OPTION`'s button, which v1 offered as a free pass
#: for an admin to wave someone through. Matched on the callback payload's
#: option half rather than the button label, which is localised.
_APPROVE = "APPROVE"


def _challenge_message(sandbox: SandboxClient, chat_id: int, since: int) -> dict[str, Any]:
    """The bot's captcha message: the first new message in the chat carrying an
    inline keyboard."""
    return wait_for(
        lambda: next(
            (
                message
                for message in messages_in(sandbox.state(), chat_id, since)
                if message["reply_markup"] is not None
            ),
            None,
        ),
        timeout=20.0,
        description="issue a captcha challenge with an inline keyboard",
        on_timeout=lambda: describe_recent_calls(sandbox.state()),
    )


def test_a_newcomer_is_challenged_and_the_challenge_replies_to_the_join(
    sandbox: SandboxClient, captcha_group_id: int, lang: str
) -> None:
    newcomer = sandbox.create_user("Cap", "cap")["id"]

    before_messages = len(sandbox.state()["messages"].get(str(captcha_group_id), []))
    before_calls = len(sandbox.state()["api_calls"])
    sandbox.join(captcha_group_id, newcomer)

    challenge = _challenge_message(sandbox, captcha_group_id, before_messages)

    # The assertion that would have caught the original bug: the challenge is a
    # *reply* to the join's service message, which is only possible if that
    # message was stored. A `sendMessage` that merely landed in the chat would
    # satisfy a weaker check while the captcha remained unreachable in practice.
    join_message = sandbox.state()["messages"][str(captcha_group_id)][before_messages]
    assert join_message["service"] == {
        "kind": "join",
        "user_id": newcomer,
        "by_user_id": None,
    }
    assert challenge["reply_to_message_id"] == join_message["message_id"]

    # And no Bot API call failed on the way: an issuance that 400s still shows
    # up in the log, so a green assertion above with a failed call below would
    # mean the bot got there by a different route than the one under test.
    assert calls_to(sandbox.state(), "sendMessage", before_calls), (
        f"the challenge never went out as a sendMessage: {describe_recent_calls(sandbox.state())}"
    )

    options = [
        button["callback_data"]
        for row in challenge["reply_markup"]["inline_keyboard"]
        for button in row
    ]
    assert any(data.endswith(_APPROVE) for data in options), (
        f"no approve button on the challenge: {options}"
    )
    # An arithmetic prompt plus the approve pass: more than one way through, or
    # the "captcha" is the free pass v1 shipped (see the port's contract).
    assert len(options) > 1, options

    # Unlike `callback_data` (wire shape, checked above), the button *label*
    # is genuinely localised (`groupguardian._captcha_strings`'s nested
    # `"captcha"` catalog object) — this is the one real content check in this
    # file that a broken `pt` captcha catalog entry would actually fail.
    approve_button = next(
        button
        for row in challenge["reply_markup"]["inline_keyboard"]
        for button in row
        if button["callback_data"].endswith(_APPROVE)
    )
    # `locales.catalog` is typed `Mapping[str, str]` for every other feature's
    # flat keys; this one entry is the nested `"captcha"` object — the same
    # declared/actual mismatch `groupguardian._captcha_strings` already casts
    # around, reproduced here rather than importing that private helper.
    captcha_strings = cast(
        dict[str, str], cast(dict[str, object], locales.catalog(lang))["captcha"]
    )
    assert approve_button["text"] == captcha_strings["button_approve"]


def test_pressing_the_approve_button_answers_the_callback(
    sandbox: SandboxClient, captcha_group_id: int
) -> None:
    """The other half of the round trip, over real HTTP: a button press becomes
    a `callback_query` update, the gateway handles it, and Telegram sees an
    `answerCallbackQuery` — the call a chat window cannot show and the one
    thing that proves the press was not silently dropped."""
    newcomer = sandbox.create_user("Cappy", "cappy")["id"]

    before_messages = len(sandbox.state()["messages"].get(str(captcha_group_id), []))
    sandbox.join(captcha_group_id, newcomer)
    challenge = _challenge_message(sandbox, captcha_group_id, before_messages)

    approve = next(
        button["callback_data"]
        for row in challenge["reply_markup"]["inline_keyboard"]
        for button in row
        if button["callback_data"].endswith(_APPROVE)
    )

    before_calls = len(sandbox.state()["api_calls"])
    sandbox.press_callback(captcha_group_id, newcomer, challenge["message_id"], approve)

    wait_for(
        lambda: calls_to(sandbox.state(), "answerCallbackQuery", before_calls) or None,
        timeout=20.0,
        description="answer the captcha callback query",
        on_timeout=lambda: describe_recent_calls(sandbox.state()),
    )
