"""Step definitions for fun_dice.

QA: qa/features/fun_dice.feature (synced from Cookiebot-QA/features/fun_dice.feature,
plus scenarios covering v1 behaviour the spec never exercises — see that file's
own header comment). Contract: docs/contracts/fun_dice.md.

Drives the real dispatcher (`cb_gateway.main.dp`, via `qa/conftest.py`'s
`dispatcher` fixture) against the mock Telegram API — no database needed:
`cb_gateway.handlers.dice` reads only `group_config` (which degrades to v1's
defaults offline, `qa/conftest.py`'s own module docstring) and never persists
anything. The one scenario that flips `functions_utility` off uses the real
`database` fixture and `group_config.set_config` (mirrors `qa/test_fun_random.py`'s
`fun_disabled` step for the sibling `functions_fun` gate) rather than
monkeypatching `group_config` itself — AGENTS.md §6 forbids mocking our own code
in an acceptance test.

QA's own wording phrases the trigger as a bare word ("roll 6"), not a literal
Telegram command — unlike every sibling `*.feature` file this port has seen
(`core_privacy.feature`, `core_rules.feature`, `fun_random.feature`: always a
literal "/word"). `cb_core/textmatch.py:COMMAND_ALIASES` maps "roll" to the
canonical `"dice"` name only for a real slash command ("/roll 6") — there is no
bare-word trigger path anywhere in this codebase (docs/FEATURE-MAP.md's own
"spec/code trigger mismatch" note for fun_dice). `_to_command` below adds the
leading "/" a real Telegram command needs without changing the Gherkin wording
itself, which stays byte-identical to the upstream spec.

NOTE: `packages/cb-gateway/src/cb_gateway/handlers/__init__.py` does not
register `dice.router` yet (out of this feature's file ownership — several
sibling ports, e.g. core_rules, fun_random, note the exact same gap). These
scenarios stay red until whoever owns that file adds
`root.include_router(dice.router)`.
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

scenarios("fun_dice.feature")

_TRAILING_NUMBER = re.compile(r"(-?\d+)\s*$")


def _to_command(text: str) -> str:
    """See the module docstring: QA phrases the trigger as a bare word."""
    return text if text.startswith("/") else f"/{text}"


@pytest.fixture(autouse=True)
def _reset_config(run: Callable[[Coroutine[Any, Any, Any]], Any]) -> Iterator[None]:
    """`group_config._l1` is process-global; the "utility off" scenario flips
    `functions_utility` and must not leak into a later scenario reusing the
    same `GROUP_ID` (same fix `qa/test_fun_random.py` applies for `functions_fun`).

    Unlike that sibling suite, most scenarios here need no database at all
    (`group_config` degrades to v1's defaults offline, `qa/conftest.py`'s own
    module docstring) -- so, unlike `fun_random`'s unconditional reset, this
    guards on a pool actually existing before touching it, the same way
    `qa/conftest.py`'s central `_clean` fixture guards its own L2/DB cleanup.
    """
    from cb_core import db

    yield
    try:
        db.pool()
    except RuntimeError:
        return  # no database in this run; nothing was ever persisted either
    run(group_config.set_config(GROUP_ID, functions_utility=True))
    group_config._l1.clear()  # noqa: SLF001


class Ctx:
    """Per-scenario state on top of qa/conftest.py's base `Context`."""

    def alloc_id(self) -> int:
        # Shared process-wide counter: a per-scenario counter collides with
        # earlier scenarios and the dedupe middleware drops the update.
        return next_update_id()


@pytest.fixture
def dice_ctx() -> Ctx:
    return Ctx()


# --------------------------------------------------------------------- given


@given("that the bot is in the group and properly set up")
def bot_set_up(ctx: Context) -> None:
    ctx.bot_running = True


@given("that the user is a member of the group")
def user_in_group() -> None:
    pass


@given("utility functions are disabled for the group")
def utility_disabled(database: Any, run: Callable[[Coroutine[Any, Any, Any]], Any]) -> None:
    """Requires the real `database` fixture (skips cleanly when unreachable,
    same as every other DB-backed acceptance scenario) -- unlike the rest of
    this suite, this one scenario really does need `group_configs` written,
    not just the offline defaults `group_config` otherwise falls back to."""
    run(group_config.set_config(GROUP_ID, functions_utility=False))


# ---------------------------------------------------------------------- when


@when(parsers.parse('the user sends the command "{command}"'))
def user_sends_command(
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
    dice_ctx: Ctx,
    command: str,
) -> None:
    feed(run, dispatcher, bot, make_message_update(_to_command(command), dice_ctx.alloc_id()))


# ---------------------------------------------------------------------- then


@then(parsers.parse("the bot should respond with a number between {low:d} and {high:d}"))
def bot_responds_in_range(telegram: MockTelegram, low: int, high: int) -> None:
    sent = telegram.calls_to("sendMessage")
    assert sent, "expected a sendMessage call, got none"
    body = sent[-1].get("text", "")
    match = _TRAILING_NUMBER.search(body)
    assert match, f"no trailing number found in {body!r}"
    value = int(match.group(1))
    assert low <= value <= high, (value, body)


@then(
    parsers.parse(
        "the bot should roll the die {times:d} times, each result between {low:d} and {high:d}"
    )
)
def bot_rolls_several_times(telegram: MockTelegram, times: int, low: int, high: int) -> None:
    sent = telegram.calls_to("sendMessage")
    assert sent, "expected a sendMessage call, got none"
    body = sent[-1].get("text", "")
    values = [int(v) for v in re.findall(r"->\s*(-?\d+)", body)]
    assert len(values) == times, (values, body)
    assert all(low <= v <= high for v in values), (values, body)


@then(
    "the bot should respond with an error message indicating that the number of sides must be specified"
)
def bot_shows_usage_example(telegram: MockTelegram) -> None:
    """v1's `dice_exemple` catalog string doubles as both "here is how to use
    this command" and, for QA's bare "roll", the closest real v1 text to "you
    must specify the number of sides" — see docs/contracts/fun_dice.md."""
    sent = telegram.calls_to("sendMessage")
    assert sent, "expected a sendMessage call, got none"
    assert sent[-1].get("text", "") == locales.get("dice_exemple", "en")


@then("the bot should display a message saying utility functions are disabled")
def bot_says_utility_off(telegram: MockTelegram) -> None:
    sent = telegram.calls_to("sendMessage")
    assert sent, "expected a sendMessage call, got none"
    assert sent[-1].get("text", "") == locales.get("utility_off", "en")


@then("the user receives no response")
def no_response(telegram: MockTelegram) -> None:
    assert not telegram.calls_to("sendMessage")
