"""Step definitions for fun_firecracker.

QA: qa/features/fun_firecracker.feature (synced from
Cookiebot-QA/features/fun_firecracker.feature, wording unchanged, plus a
second scenario for the fun-off gate that the upstream spec never exercises —
see that file's own header comment). Contract: docs/contracts/fun_firecracker.md.

Drives the real dispatcher (`cb_gateway.main.dp`, via `qa/conftest.py`'s
`dispatcher` fixture) against the mock Telegram API — no database needed for
the happy path: `cb_gateway.handlers.firecracker` reads only `group_config`
(which degrades to v1's defaults offline, `qa/conftest.py`'s own module
docstring) and never persists anything. The fun-off scenario needs the real
`database` fixture to flip `functions_fun`, the same pattern
`qa/test_fun_dice.py`'s `utility_disabled` step and `qa/test_fun_ship.py`'s
`fun_disabled` step use for their own gates — AGENTS.md §6 forbids mocking our
own code in an acceptance test.

Every `feed()` call takes a fresh id from `next_update_id()` (never a
hand-rolled counter): the dispatcher under test is session-scoped and carries
the real dedupe middleware, so a reused update_id is dropped as a Telegram
redelivery and reads as "the bot said nothing" (qa/conftest.py's own note on
`next_update_id`).

The handler's `await asyncio.sleep(0.1)` between the fuse and the burst
(design R4.4, Miscellaneous.py:229) runs for real here — it is not
monkeypatched away. Two scenarios cost ~0.2s total, nowhere near making this
suite slow enough to justify faking v1's timing.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Coroutine, Iterator
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from pytest_bdd import given, parsers, scenarios, then, when

from cb_core import group_config, locales
from qa.conftest import GROUP_ID, Context, feed, make_message_update, next_update_id
from qa.mock_telegram import MockTelegram

scenarios("fun_firecracker.feature")

# Miscellaneous.py:236 -- every burst line is "pra " repeated n>=1 times, and
# nothing else ever looks like this, so the pattern alone is enough to pick
# burst lines out of the full sendMessage sequence.
_BURST_LINE = re.compile(r"^(pra )+$")


@pytest.fixture(autouse=True)
def _reset_config(run: Callable[[Coroutine[Any, Any, Any]], Any]) -> Iterator[None]:
    """`group_config._l1` is process-global; the "fun off" scenario flips
    `functions_fun` on the shared `GROUP_ID` and must not leak into a later
    scenario (same fix `qa/test_fun_dice.py` and `qa/test_fun_ship.py` apply
    for their own gates). Guards on a pool actually existing first, since the
    happy-path scenario needs no database at all."""
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

    def alloc_id(self) -> int:
        return next_update_id()


@pytest.fixture
def firecracker_ctx() -> Ctx:
    return Ctx()


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


# ---------------------------------------------------------------------- when


@when(parsers.parse('the user types the command "{command}"'))
def user_types_command(
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
    firecracker_ctx: Ctx,
    command: str,
) -> None:
    feed(run, dispatcher, bot, make_message_update(command, firecracker_ctx.alloc_id()))


# ---------------------------------------------------------------------- then


@then("the bot should send multiple firecracker messages in a sequence to the group")
def bot_sends_firecracker_sequence(telegram: MockTelegram) -> None:
    sent = telegram.calls_to("sendMessage")
    assert len(sent) >= 3, sent  # fuse + at least one burst line + bang
    texts = [str(call.get("text", "")) for call in sent]
    assert texts[0] == "fiiiiiiii.... ", texts
    assert any(_BURST_LINE.match(text) for text in texts[1:-1]), texts
    assert texts[-1] == "<b> \U0001f4a5POOOOOOOWW\U0001f4a5 </b>", texts


@then("the bot should reply with a message saying that the fun feature is turned off")
def bot_says_fun_off(telegram: MockTelegram) -> None:
    sent = telegram.calls_to("sendMessage")
    assert sent, "expected a sendMessage call, got none"
    assert len(sent) == 1, sent  # gated off: one reply, nothing else
    assert str(sent[-1].get("text", "")) == locales.get("fun_off", "en")
