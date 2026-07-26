"""Step definitions for util_doomlist.

QA: qa/features/util_doomlist.feature (synced from
../Cookiebot-QA/features/util_doomlist.feature, plus scenarios added while
porting — see that file's own comment for which ones and why).
Contract: docs/contracts/util_doomlist.md.

Drives the real dispatcher (`cb_gateway.main.dp`, via `qa/conftest.py`'s
`dispatcher` fixture) against the mock Telegram API, same as every other
acceptance test in this suite. Two things are faked here, both at the outside
world's boundary, never our own code (AGENTS.md §6):

- The two external HTTP dependencies (cas.chat, burrbot.xyz) are stubbed via
  `doomlist.set_http_client` with an `httpx.MockTransport` — that boundary
  stubbing is explicitly required, not just allowed, per this port's task
  brief, and is exactly what "the third party is down" scenarios below need.
- Nothing about `check_local_blacklist` itself is faked: it reads the real
  `blacklist`/`users` reference tables, which is why this whole file depends
  on the `database` fixture (same reasoning `qa/test_core_welcome.py` gives
  for `group_welcomes`).

NOTE: `packages/cb-gateway/src/cb_gateway/handlers/__init__.py` does not
register `doomlist.router` yet (out of this feature's file ownership — see the
task's file list; `rules.router`/`welcome.router` weren't registered when
their own test files were written either, per those files' own docstrings).
`doomlist.router` must additionally be registered *before* `welcome.router`
(see `doomlist.py`'s module docstring and `docs/contracts/util_doomlist.md`'s
"Wiring note") for these scenarios to observe a block rather than a welcome.
Until both are true, these scenarios exercise the real handler logic but
cannot pass end to end against the shared `dispatcher` fixture — the same
documented, accepted state as `qa/test_core_rules.py` and
`qa/test_core_welcome.py` for their own routers.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Iterator
from types import ModuleType
from typing import Any

import httpx
import pytest
from aiogram import Bot, Dispatcher
from pytest_bdd import given, parsers, scenarios, then, when

from cb_core import db, group_config
from cb_gateway.handlers import doomlist
from qa.conftest import (
    GROUP_ID,
    NEWCOMER_ID,
    USER_ID,
    Context,
    feed,
    make_join_update,
    next_update_id,
)
from qa.mock_telegram import MockTelegram

scenarios("util_doomlist.feature")


# ------------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
def _real_blacklist_table(database: ModuleType) -> None:
    """`check_local_blacklist` reads the real `blacklist`/`users` reference
    tables on every non-forbidden-character, non-CAS-hit path — faking that
    seam would leave a scenario asserting only that the handler can echo back
    a row it just wrote itself. `database` skips the whole suite when no
    Postgres is reachable (AGENTS.md §6).
    """


@pytest.fixture(autouse=True)
def _reset_doomlist_state(run: Callable[[Coroutine[Any, Any, Any]], Any]) -> Iterator[None]:
    """Every breaker, the injected HTTP client, and `doomlist_enabled` are
    process/database-global state this module (or the shared `group_configs`
    row) owns — reset around each scenario so one scenario's "the service is
    down" or "the feature is disabled" doesn't leak into the next. Mirrors the
    idiom `qa/conftest.py` already uses for `admins._l1`/`group_config._l1`.
    """
    doomlist._cas_breaker = doomlist.Breaker()  # noqa: SLF001
    doomlist._burrbot_breaker = doomlist.Breaker()  # noqa: SLF001
    doomlist.set_http_client(_stub_transport(cas_hit=False, burrbot_hit=False))
    run(group_config.set_config(GROUP_ID, doomlist_enabled=True))
    yield
    run(
        db.execute(
            "DELETE FROM blacklist WHERE subject_id = $1",
            NEWCOMER_ID,
            name="test_doomlist_cleanup",
        )
    )
    doomlist.set_http_client(None)


def _stub_transport(
    *, cas_hit: bool, burrbot_hit: bool, cas_down: bool = False, burrbot_down: bool = False
) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.cas.chat":
            if cas_down:
                raise httpx.ConnectError("simulated cas.chat outage", request=request)
            return httpx.Response(200, json={"ok": cas_hit})
        if request.url.host == "burrbot.xyz":
            if burrbot_down:
                raise httpx.ConnectError("simulated burrbot outage", request=request)
            return httpx.Response(200, text='{"raider": %s}' % ("true" if burrbot_hit else "false"))
        raise AssertionError(f"unexpected host in doomlist test stub: {request.url.host}")

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class Ctx:
    def __init__(self) -> None:
        self.newcomer_name = "Newcomer"

    def alloc_id(self) -> int:
        return next_update_id()


@pytest.fixture
def doomlist_ctx() -> Ctx:
    return Ctx()


# --------------------------------------------------------------------- given


@given("that the group has the Doomlist feature enabled")
def doomlist_feature_enabled(run: Callable[[Coroutine[Any, Any, Any]], Any]) -> None:
    run(group_config.set_config(GROUP_ID, doomlist_enabled=True))


@given("that the group has the Doomlist feature disabled")
def doomlist_feature_disabled(run: Callable[[Coroutine[Any, Any, Any]], Any]) -> None:
    run(group_config.set_config(GROUP_ID, doomlist_enabled=False))


@given("the bot is properly set with this feature enabled")
def bot_set_up(ctx: Context) -> None:
    ctx.bot_running = True


@given("that the user is listed on the Doomlist")
def user_listed_on_doomlist(run: Callable[[Coroutine[Any, Any, Any]], Any]) -> None:
    run(
        db.execute(
            "INSERT INTO blacklist (subject_id, kind, source) VALUES ($1, 'user', 'manual') "
            "ON CONFLICT (subject_id) DO NOTHING",
            NEWCOMER_ID,
            name="test_seed_doomlist",
        )
    )


@given("that the user is flagged by the CAS anti-spam service")
def user_flagged_by_cas() -> None:
    doomlist.set_http_client(_stub_transport(cas_hit=True, burrbot_hit=False))


@given("that the user is flagged by the public raid-block service")
def user_flagged_by_burrbot() -> None:
    doomlist.set_http_client(_stub_transport(cas_hit=False, burrbot_hit=True))


@given("that the user's display name contains a forbidden character")
def user_name_has_forbidden_char(doomlist_ctx: Ctx) -> None:
    # GroupShield.py:210's swastika glyph — copied verbatim in doomlist.py's
    # own `_FORBIDDEN_NAME_CHARS`.
    doomlist_ctx.newcomer_name = "Raider卐"


@given("that the user is not listed anywhere")
def user_not_listed_anywhere() -> None:
    doomlist.set_http_client(_stub_transport(cas_hit=False, burrbot_hit=False))


@given("both the CAS anti-spam service and the public raid-block service are down")
def both_external_services_down() -> None:
    doomlist.set_http_client(
        _stub_transport(cas_hit=False, burrbot_hit=False, cas_down=True, burrbot_down=True)
    )


# ---------------------------------------------------------------------- when


@when("they try to join the group with the bot enabled")
def they_try_to_join(
    doomlist_ctx: Ctx,
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
) -> None:
    update_id = doomlist_ctx.alloc_id()
    feed(
        run,
        dispatcher,
        bot,
        make_join_update(
            update_id,
            joiners=[(NEWCOMER_ID, doomlist_ctx.newcomer_name)],
            by_user_id=NEWCOMER_ID,  # a self-join, matching COOKIEBOT.py:136's precondition
        ),
    )


@when("an existing member adds them to the group instead of them joining themself")
def someone_else_adds_them(
    doomlist_ctx: Ctx,
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
) -> None:
    update_id = doomlist_ctx.alloc_id()
    feed(
        run,
        dispatcher,
        bot,
        make_join_update(
            update_id,
            joiners=[(NEWCOMER_ID, doomlist_ctx.newcomer_name)],
            by_user_id=USER_ID,  # different from the joiner -> not a self-join
        ),
    )


# ---------------------------------------------------------------------- then


@then("they should be prevented from joining the group")
def should_be_banned(telegram: MockTelegram) -> None:
    banned = telegram.calls_to("banChatMember")
    assert banned, "expected a banChatMember call, got none"
    assert int(banned[-1].get("user_id", 0)) == NEWCOMER_ID, banned[-1]


@then("they should not be prevented from joining the group")
def should_not_be_banned(telegram: MockTelegram) -> None:
    banned = [
        c for c in telegram.calls_to("banChatMember") if int(c.get("user_id", 0)) == NEWCOMER_ID
    ]
    assert not banned, banned


@then(parsers.parse('the bot should send a message on the group saying "{text}"'))
def bot_says_on_group(telegram: MockTelegram, text: str) -> None:
    sent = telegram.calls_to("sendMessage")
    assert sent, "expected a sendMessage call, got none"
    assert sent[-1].get("text", "") == text, sent[-1]
