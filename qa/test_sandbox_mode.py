"""Proves `CB_QA_SANDBOX=1` does what docs/site/content/docs/sandbox.mdx promises: after the
suite runs, `tg_sandbox.state.store()` — the same store the sandbox's own web
UI reads — holds the user's message, the bot's reply, and the matching
`api_calls` entry.

Skipped unless the flag is set: this is not part of the CI gate (that is
still `qa/test_*.py` against `qa/mock_telegram.py`, unmodified and unslowed —
see `qa/conftest.py`'s `telegram` fixture). This file exists to verify the
*other* mode, not to add to the default one.

`/privacy` (`qa/test_core_privacy.py`) is the vehicle: no database, no
Valkey, no admin rights needed, so a failure here can only be about the
sandbox plumbing (`qa/sandbox_harness.py`), never about infra this file does
not control.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from qa.conftest import ADMIN_ID, GROUP_ID, USER_ID, feed, make_message_update, next_update_id
from qa.sandbox_harness import sandbox_enabled

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from aiogram import Bot, Dispatcher

pytestmark = pytest.mark.skipif(
    not sandbox_enabled(), reason="only meaningful with CB_QA_SANDBOX=1 (see qa/sandbox_harness.py)"
)

_PRIVACY_URL = "https://cookiebotfur.net/privacy"


def _send_privacy_and_check(
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
    *,
    user_id: int,
) -> None:
    """Feed one `/privacy` from `user_id` and assert the round trip landed in
    the store — as *new* entries, not merely "somewhere in history". The
    store is cumulative across the whole session by design (that is the
    point of sandbox mode: a human reads it after `cb.py test` finishes), so
    a plain "does a matching entry exist" check would pass even if this
    scenario's own traffic never reached the store at all, as long as an
    earlier scenario's did. Snapshotting the store's length before acting and
    only inspecting what was appended keeps the assertion honest regardless
    of what ran before it or whether this file runs alone.
    """
    from tg_sandbox.state import store

    s = store()
    messages_before = len(s.messages.get(GROUP_ID, []))
    calls_before = len(s.api_calls)

    feed(run, dispatcher, bot, make_message_update("/privacy", next_update_id(), user_id=user_id))

    new_messages = s.messages.get(GROUP_ID, [])[messages_before:]
    new_calls = s.api_calls[calls_before:]

    user_messages = [m for m in new_messages if m.from_id == user_id]
    assert any(m.text == "/privacy" for m in user_messages), (
        f"the user's own message never reached the sandbox store: {new_messages}"
    )

    bot_replies = [m for m in new_messages if m.from_id != user_id and m.text]
    assert any(_PRIVACY_URL in (m.text or "") for m in bot_replies), (
        f"the bot's reply never reached the sandbox store: {new_messages}"
    )

    send_calls = [c for c in new_calls if c["method"] == "sendMessage"]
    assert any(
        _PRIVACY_URL in c["payload"].get("text", "")
        and int(c["payload"].get("chat_id", 0)) == GROUP_ID
        for c in send_calls
    ), f"no matching sendMessage in api_calls: {new_calls}"


def test_a_scenarios_traffic_lands_in_the_sandbox_store(
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
) -> None:
    """One round trip — a user's `/privacy` and the bot's reply — proves both
    directions actually reach the store: `qa/conftest.py:feed()` mirrors the
    inbound message (bypassing `tg_sandbox.control_api`, which the harness
    never drives), and the outbound `sendMessage` is the real
    `tg_sandbox.app:app` recording its own call, unmodified.
    """
    _send_privacy_and_check(run, dispatcher, bot, user_id=USER_ID)


def test_a_second_users_traffic_is_kept_distinct(
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
) -> None:
    """A second scenario's worth of traffic, from a different user
    (`ADMIN_ID`, already an administrator per `qa/conftest.py`'s autouse
    `_clean` fixture), round-trips the same way — its own message, its own
    reply, its own `api_calls` entry — rather than being merged into or
    dropped by the first test's.
    """
    _send_privacy_and_check(run, dispatcher, bot, user_id=ADMIN_ID)
