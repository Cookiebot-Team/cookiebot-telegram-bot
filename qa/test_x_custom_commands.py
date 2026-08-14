"""Step definitions for x_custom_commands.

QA: qa/features/x_custom_commands.feature (authored locally — see the feature
file's header for why upstream QA could not have one). Contract:
docs/contracts/x_custom_commands.md.

Fakes the pool (`legacy_assets.entries_for_custom`, which is both the trigger
list and the images) and seeds real bytes for the two rows a scenario can
receive. The handler, the filter, the pack lookup and the storage read all run
for real.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Coroutine, Iterator
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from pytest_bdd import given, parsers, scenarios, then, when

from cb_core import group_config, legacy_assets, locales, tenancy
from cb_core.legacy_assets import LegacyAsset
from cb_core.settings import Settings
from qa.conftest import GROUP_ID, Context, feed, make_message_update, next_update_id
from qa.mock_telegram import MockTelegram

scenarios("x_custom_commands.feature")

POOL_NAME = "louie"

_BYTES = (b"qa custom pool image zero", b"qa custom pool image one")

_POOL = tuple(
    LegacyAsset(
        source_path=f"Custom/{POOL_NAME}/{index}.jpg",
        destination_key=f"legacy/v1-bucket/qa/qa-custom-{index}.jpg",
        byte_size=len(payload),
        content_hash=f"qa-custom-{index}",
    )
    for index, payload in enumerate(_BYTES)
)


@pytest.fixture(scope="module", autouse=True)
def _pool_storage(run: Callable[[Coroutine[Any, Any, Any]], Any]) -> Iterator[None]:
    from cb_core import storage

    already_initialised = True
    try:
        storage.store()
    except RuntimeError:
        already_initialised = False

    if not already_initialised:
        run(storage.init_storage(Settings(service_name="cb-qa-custom", traces_enabled=False)))
    for entry, payload in zip(_POOL, _BYTES, strict=True):
        run(storage.store().put(entry.storage_key, payload))
    yield
    if not already_initialised:
        run(storage.close_storage())


class Ctx:
    def alloc_id(self) -> int:
        return next_update_id()


@pytest.fixture
def custom_ctx() -> Ctx:
    return Ctx()


@pytest.fixture(autouse=True)
def _patch_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    """One pool, named `louie` — a real folder name from the export, so the
    scenario reads like a group's actual message."""
    monkeypatch.setattr(
        legacy_assets,
        "entries_for_custom",
        lambda name: _POOL if name == POOL_NAME else (),
    )


@pytest.fixture(autouse=True)
def _restore_switch(run: Callable[[Coroutine[Any, Any, Any]], Any]) -> Iterator[None]:
    yield
    with contextlib.suppress(Exception):
        run(group_config.set_config(GROUP_ID, functions_fun=True))
    group_config._l1.clear()  # noqa: SLF001


# --------------------------------------------------------------------- given


@given("that the bot is in the group and properly set up")
def bot_set_up(ctx: Context) -> None:
    ctx.bot_running = True


@given("that the user is a member of the group")
def user_is_member() -> None:
    """Nothing to arrange — v1 has no admin or membership check here."""


@given("the fun feature is turned off")
def fun_disabled(database: Any, run: Callable[[Coroutine[Any, Any, Any]], Any]) -> None:
    run(group_config.set_config(GROUP_ID, functions_fun=False))
    group_config._l1.clear()  # noqa: SLF001


@given("the brand runs the minimal handler pack")
def minimal_pack(monkeypatch: pytest.MonkeyPatch) -> None:
    """`tenants.handler_pack`, finally read by something
    (`cb_gateway/packs.py`)."""

    async def _minimal(skin: str) -> tenancy.Tenant:
        return tenancy.Tenant(tenant_id="brand", display_name="Brand", handler_pack="minimal")

    monkeypatch.setattr(tenancy.registry, "by_skin", _minimal)


# ---------------------------------------------------------------------- when


@when(parsers.parse('the user types the command "{command}"'))
def user_types_command(
    custom_ctx: Ctx,
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
    command: str,
) -> None:
    feed(run, dispatcher, bot, make_message_update(command, custom_ctx.alloc_id()))


# ---------------------------------------------------------------------- then


@then("the bot should reply with a picture captioned with the pool's name and id")
def bot_sends_a_custom_picture(telegram: MockTelegram) -> None:
    calls = telegram.calls_to("sendPhoto")
    assert calls, f"expected a sendPhoto call, got {telegram.calls}"
    caption = str(calls[-1].get("caption", ""))
    assert caption in {
        locales.get("custom_photo", "en", name="Louie", image_id=index)
        for index in range(len(_POOL))
    }, caption


@then("the bot should reply with picture number 1")
def bot_sends_picture_one(telegram: MockTelegram) -> None:
    calls = telegram.calls_to("sendPhoto")
    assert calls, "expected a sendPhoto call"
    assert str(calls[-1].get("caption", "")) == locales.get(
        "custom_photo", "en", name="Louie", image_id=1
    )


@then("the bot should reply that fun functions are disabled")
def bot_says_fun_off(telegram: MockTelegram) -> None:
    sent = telegram.calls_to("sendMessage")
    assert sent, "expected a sendMessage call"
    assert str(sent[-1].get("text", "")) == locales.get("fun_off", "en")
    assert not telegram.calls_to("sendPhoto")


@then("the bot should send nothing at all")
def bot_sends_nothing(telegram: MockTelegram) -> None:
    assert not telegram.calls_to("sendPhoto")
    assert not telegram.calls_to("sendMessage")
