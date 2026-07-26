"""Step definitions for core_groupguardian.

QA: qa/features/core_groupguardian.feature (copied verbatim from
Cookiebot-QA/features/core_groupguardian.feature, plus scenarios added for v1
behaviour the original spec did not exercise — see the feature file's own
comment block and docs/contracts/core_groupguardian.md).

Drives the real dispatcher (`cb_gateway.main.dp`, via `qa/conftest.py`'s
`dispatcher` fixture) against the mock Telegram API. The one thing faked here
is nothing: `captcha_challenges` is the real table (`clean_captcha` fixture,
AGENTS.md §6 forbids mocking our own code in an acceptance test), and the
`_welcome_text`/`_send_welcome_text` calls this feature makes on a successful
solve (v1: `solve_captcha` -> `welcome_message`) run for real too, reading the
real (empty) `group_welcomes` row for the QA group.

NOTE: `packages/cb-gateway/src/cb_gateway/handlers/__init__.py` does not
register `groupguardian.router` yet (out of this feature's file ownership —
see the task's file list). It must be registered **before**
`welcome.router`: `on_join` uses `SkipHandler` to defer to `welcome.on_join`
whenever the captcha does not apply (invited join, gate closed), which only
works if this router gets first refusal on `new_chat_members`
(`docs/contracts/core_groupguardian.md` and `docs/contracts/core_welcome.md`
both flag this same dependency). Until that wiring lands, every scenario here
fails as "the bot said nothing" — not a defect in this port.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from cb_core import admins as admins_module
from cb_core import captcha as captcha_module
from cb_core import db, group_config
from cb_gateway.handlers import groupguardian as gg
from qa.conftest import (
    ADMIN_ID,
    GROUP_ID,
    NEWCOMER_ID,
    feed,
    make_callback_update,
    make_join_update,
    make_message_update,
    next_update_id,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Iterator, Mapping

    from aiogram import Bot, Dispatcher

    from qa.conftest import Context
    from qa.mock_telegram import MockTelegram

scenarios("core_groupguardian.feature")

# The mock's `getMe` id (qa/mock_telegram.py) — used here to make the bot its
# own group's admin, the second half of v1's gate (`COOKIEBOT.py:147`).
BOT_ID = 424242


@pytest.fixture(autouse=True)
def _real_captcha_table(clean_captcha: None) -> None:
    """The real `captcha_challenges` table, truncated for this group around
    each scenario — same rationale as `qa/test_core_rules.py`'s
    `_real_rules_table`."""


@pytest.fixture(autouse=True)
def _fresh_caches() -> Iterator[None]:
    """`cb_core.admins._l1` / `cb_core.group_config._l1` are process-global,
    TTL'd dicts; every scenario in this file reuses `GROUP_ID`
    (`qa/conftest.py`), so a previous scenario's admin set or config change
    would otherwise leak in for up to `config_cache_l1_seconds` — same fix
    `qa/test_core_rules.py` applies for the same reason."""
    admins_module._l1.clear()  # noqa: SLF001
    group_config._l1.clear()  # noqa: SLF001
    yield
    admins_module._l1.clear()  # noqa: SLF001
    group_config._l1.clear()  # noqa: SLF001


class Ctx:
    """Extra per-scenario state on top of qa/conftest.py's base `Context`."""

    def __init__(self) -> None:
        self.joiner_id = NEWCOMER_ID


@pytest.fixture
def gg_ctx() -> Ctx:
    return Ctx()


def _pending_row(run: Callable[[Coroutine[Any, Any, Any]], Any]) -> Mapping[str, Any]:
    row = run(gg._fetch_pending(GROUP_ID, NEWCOMER_ID))  # noqa: SLF001
    assert row is not None, "no pending captcha challenge for the newcomer"
    return row


# --------------------------------------------------------------------- given


@given("that the group is protected by Cookiebot")
def group_protected(ctx: Context) -> None:
    ctx.bot_running = True


@given("the bot is properly set with this feature")
def bot_set_up(telegram: MockTelegram) -> None:
    """v1's gate: `captchatimespan > 0` (the group_configs default, 300s) and
    `myself['username'] in listaadmins` — the bot must be an admin of its own
    group (`COOKIEBOT.py:147`)."""
    telegram.set_admins(GROUP_ID, [(ADMIN_ID, "administrator"), (BOT_ID, "administrator")])


@given("that the user is not a member of the group")
def user_not_a_member() -> None:
    """Documents the precondition; nothing to seed — a fresh Telegram user
    with no prior `group_members` row is exactly what a join event models."""


@given("the user tries to join the group")
def user_joins(
    run: Callable[[Coroutine[Any, Any, Any]], Any], dispatcher: Dispatcher, bot: Bot, gg_ctx: Ctx
) -> None:
    feed(
        run,
        dispatcher,
        bot,
        make_join_update(
            next_update_id(),
            joiners=[(gg_ctx.joiner_id, "Newcomer")],
            by_user_id=gg_ctx.joiner_id,
        ),
    )


@given("an existing member adds the user to the group")
def someone_else_adds_member(
    run: Callable[[Coroutine[Any, Any, Any]], Any], dispatcher: Dispatcher, bot: Bot, gg_ctx: Ctx
) -> None:
    """v1: `msg['from']['id'] != msg['new_chat_participant']['id']` -> always
    `welcome_message`, captcha never considered (`COOKIEBOT.py:136-141`)."""
    feed(
        run,
        dispatcher,
        bot,
        make_join_update(
            next_update_id(),
            joiners=[(gg_ctx.joiner_id, "Newcomer")],
            by_user_id=ADMIN_ID,
        ),
    )


@given("the group has the captcha feature disabled")
def captcha_disabled(run: Callable[[Coroutine[Any, Any, Any]], Any]) -> None:
    run(group_config.set_config(GROUP_ID, captcha_timeout_seconds=0))


@given("the bot is not an admin of the group")
def bot_not_admin(telegram: MockTelegram) -> None:
    telegram.set_admins(GROUP_ID, [(ADMIN_ID, "administrator")])


# ---------------------------------------------------------------------- when


@when("they solve the captcha challenge")
def solve_correctly(
    run: Callable[[Coroutine[Any, Any, Any]], Any], dispatcher: Dispatcher, bot: Bot
) -> None:
    row = _pending_row(run)
    feed(
        run,
        dispatcher,
        bot,
        make_message_update(row["answer"], next_update_id(), user_id=NEWCOMER_ID),
    )


@when("they answer the captcha challenge incorrectly")
def answer_wrong_once(
    run: Callable[[Coroutine[Any, Any, Any]], Any], dispatcher: Dispatcher, bot: Bot
) -> None:
    feed(run, dispatcher, bot, make_message_update("0000", next_update_id(), user_id=NEWCOMER_ID))


@when(parsers.parse("they answer the captcha challenge incorrectly {n:d} times"))
def answer_wrong_n_times(
    run: Callable[[Coroutine[Any, Any, Any]], Any], dispatcher: Dispatcher, bot: Bot, n: int
) -> None:
    for _ in range(n):
        feed(
            run, dispatcher, bot, make_message_update("0000", next_update_id(), user_id=NEWCOMER_ID)
        )


@when("they fail to solve the captcha challenge correctly or timeouts")
def fail_or_timeout(
    run: Callable[[Coroutine[Any, Any, Any]], Any], dispatcher: Dispatcher, bot: Bot
) -> None:
    """Exercises the "timeout" half of this scenario: v1's timer fires
    without waiting for a further message; v2 has no proactive kick-on-expiry
    (see docs/contracts/core_groupguardian.md, cb-worker gap), so this ages
    the row past `expires_at` directly, then sends one more message — the
    same "any next message re-checks the pending challenge" mechanism v1's
    `check_captcha` used (`GroupShield.py:280-311`, invoked on every
    unmatched message, not only on the timer)."""
    run(
        db.execute(
            "UPDATE captcha_challenges SET expires_at = now() - interval '1 second' "
            "WHERE group_id = $1 AND user_id = $2",
            GROUP_ID,
            NEWCOMER_ID,
            name="qa_age_out_captcha",
        )
    )
    feed(run, dispatcher, bot, make_message_update("0000", next_update_id(), user_id=NEWCOMER_ID))


@when("an admin presses the approve button")
def admin_approves(
    run: Callable[[Coroutine[Any, Any, Any]], Any], dispatcher: Dispatcher, bot: Bot
) -> None:
    row = _pending_row(run)
    data = captcha_module.callback_payload(row["nonce"], gg._APPROVE_OPTION)  # noqa: SLF001
    feed(
        run,
        dispatcher,
        bot,
        make_callback_update(
            data, next_update_id(), user_id=ADMIN_ID, message_id=row["message_id"]
        ),
    )


@when("the newcomer presses the admin-only approve button")
def newcomer_self_approves(
    run: Callable[[Coroutine[Any, Any, Any]], Any], dispatcher: Dispatcher, bot: Bot
) -> None:
    row = _pending_row(run)
    data = captcha_module.callback_payload(row["nonce"], gg._APPROVE_OPTION)  # noqa: SLF001
    feed(
        run,
        dispatcher,
        bot,
        make_callback_update(
            data, next_update_id(), user_id=NEWCOMER_ID, message_id=row["message_id"]
        ),
    )


# ---------------------------------------------------------------------- then


@then("they should be able to join the group successfully")
def joined_successfully(
    run: Callable[[Coroutine[Any, Any, Any]], Any], telegram: MockTelegram
) -> None:
    row = run(gg._fetch_pending(GROUP_ID, NEWCOMER_ID))  # noqa: SLF001
    assert row is None, "challenge is still pending, the user was not let in"
    assert not telegram.calls_to("banChatMember"), telegram.calls_to("banChatMember")


@then("they should not be able to join the group")
def not_joined(run: Callable[[Coroutine[Any, Any, Any]], Any], telegram: MockTelegram) -> None:
    """Covers both ways this feature keeps someone out: kicked (row deleted,
    `banChatMember` called — the timeout/attempts-exhausted scenarios) and
    simply never let in (row still pending, no ban — the "newcomer cannot
    self-approve" scenario). Either is "not joined"; `_succeed` is the only
    path that deletes the row *without* a ban, so the OR below rules that out.
    """
    row = run(gg._fetch_pending(GROUP_ID, NEWCOMER_ID))  # noqa: SLF001
    banned = bool(telegram.calls_to("banChatMember"))
    assert row is not None or banned, "the user appears to have joined successfully"


@then("they are told the password is incorrect and are not kicked yet")
def wrong_password_not_kicked(
    run: Callable[[Coroutine[Any, Any, Any]], Any], telegram: MockTelegram
) -> None:
    assert not telegram.calls_to("banChatMember")
    sent = telegram.calls_to("sendMessage")
    assert any(call.get("text") == gg.WRONG_ANSWER_TEXT for call in sent), sent
    row = run(gg._fetch_pending(GROUP_ID, NEWCOMER_ID))  # noqa: SLF001
    assert row is not None
    assert row["attempts"] == 1


@then("no captcha challenge is shown to the user")
def no_captcha_shown(run: Callable[[Coroutine[Any, Any, Any]], Any]) -> None:
    row = run(gg._fetch_pending(GROUP_ID, NEWCOMER_ID))  # noqa: SLF001
    assert row is None, "a captcha challenge was issued when it should not have been"
