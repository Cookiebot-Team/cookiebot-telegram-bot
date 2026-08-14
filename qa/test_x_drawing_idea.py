"""Step definitions for x_drawing_idea.

QA: qa/features/x_drawing_idea.feature (authored locally — no upstream QA
scenario exists; see the feature file's header). Contract:
docs/contracts/x_drawing_idea.md.

Fakes the same two things `qa/test_fun_death.py` does and for the same reason:
the pool the handler indexes and the bytes behind the row it picks. Here the
patch target is `legacy_assets.entries_for` rather than `choose`, because the
caption prints the *index* into that list and the handler therefore draws the
index itself.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Coroutine, Iterator
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from pytest_bdd import given, parsers, scenarios, then, when

from cb_core import group_config, legacy_assets, locales
from cb_core.legacy_assets import LegacyAsset
from cb_core.settings import Settings
from qa.conftest import GROUP_ID, Context, feed, make_message_update, next_update_id
from qa.mock_telegram import MockTelegram

scenarios("x_drawing_idea.feature")

_REFERENCE_BYTES = b"qa fake png bytes for a drawing reference"

_REFERENCE = LegacyAsset(
    source_path="IdeiaDesenho/10003.png",
    destination_key="legacy/v1-bucket/qa/qa-drawing-idea.png",
    byte_size=len(_REFERENCE_BYTES),
    content_hash="qa-drawing-idea",
)


@pytest.fixture(scope="module", autouse=True)
def _reference_storage(run: Callable[[Coroutine[Any, Any, Any]], Any]) -> Iterator[None]:
    from cb_core import storage

    already_initialised = True
    try:
        storage.store()
    except RuntimeError:
        already_initialised = False

    if not already_initialised:
        run(storage.init_storage(Settings(service_name="cb-qa-drawing", traces_enabled=False)))
    run(storage.store().put(_REFERENCE.storage_key, _REFERENCE_BYTES))
    yield
    if not already_initialised:
        run(storage.close_storage())


class Ctx:
    def __init__(self) -> None:
        self.pool: tuple[LegacyAsset, ...] = (_REFERENCE,)

    def alloc_id(self) -> int:
        return next_update_id()


@pytest.fixture
def idea_ctx() -> Ctx:
    return Ctx()


@pytest.fixture(autouse=True)
def _patch_pool(idea_ctx: Ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(legacy_assets, "entries_for", lambda *_a, **_kw: idea_ctx.pool)


@pytest.fixture(autouse=True)
def _restore_switch(run: Callable[[Coroutine[Any, Any, Any]], Any]) -> Iterator[None]:
    yield
    with contextlib.suppress(Exception):
        run(group_config.set_config(GROUP_ID, functions_utility=True))
    group_config._l1.clear()  # noqa: SLF001


# --------------------------------------------------------------------- given


@given("that the bot is in the group and properly set up")
def bot_set_up(ctx: Context) -> None:
    ctx.bot_running = True


@given("that the user is a member of the group")
def user_is_member() -> None:
    """Nothing to arrange — this command reads no registry."""


@given("the utility feature is turned off")
def utility_disabled(database: Any, run: Callable[[Coroutine[Any, Any, Any]], Any]) -> None:
    run(group_config.set_config(GROUP_ID, functions_utility=False))
    group_config._l1.clear()  # noqa: SLF001


@given("the reference pool is empty")
def pool_is_empty(idea_ctx: Ctx) -> None:
    idea_ctx.pool = ()


# ---------------------------------------------------------------------- when


@when(parsers.parse('the user types the command "{command}"'))
def user_types_command(
    idea_ctx: Ctx,
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
    command: str,
) -> None:
    feed(run, dispatcher, bot, make_message_update(command, idea_ctx.alloc_id()))


# ---------------------------------------------------------------------- then


@then("the bot should reply with a reference picture captioned with its id")
def bot_sends_a_reference(telegram: MockTelegram) -> None:
    calls = telegram.calls_to("sendPhoto")
    assert calls, f"expected a sendPhoto call, got {telegram.calls}"
    caption = str(calls[-1].get("caption", ""))
    # A one-entry pool can only ever draw index 0, which is what the caption
    # prints — the "Reference ID" is a position, not a stored identity.
    assert caption == locales.get("drawing_idea", "en", idea_id=0), caption


@then("the bot should reply that utility functions are disabled")
def bot_says_utility_off(telegram: MockTelegram) -> None:
    sent = telegram.calls_to("sendMessage")
    assert sent, "expected a sendMessage call"
    assert str(sent[-1].get("text", "")) == locales.get("utility_off", "en")
    assert not telegram.calls_to("sendPhoto")


@then("the bot should send nothing at all")
def bot_sends_nothing(telegram: MockTelegram) -> None:
    assert not telegram.calls_to("sendPhoto")
    assert not telegram.calls_to("sendMessage")
