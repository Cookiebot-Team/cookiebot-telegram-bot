"""Step definitions for fun_partneredcons.

QA: qa/features/fun_partneredcons.feature (synced from
Cookiebot-QA/features/fun_partneredcons.feature; see that file's header for the
duplicated scenario QA has and this one does not). Contract:
docs/contracts/fun_partneredcons.md.

Same two seams `qa/test_fun_death.py` uses, for the same reasons:
`legacy_assets.choose` is monkeypatched to one fixed entry so a scenario can
assert *which* picture arrived, and its bytes are seeded into a real
`memory://` store. Everything else — the dispatcher, the handler, the caption
maths — runs for real.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Coroutine, Iterator
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from pytest_bdd import given, parsers, scenarios, then, when

from cb_core import group_config, legacy_assets
from cb_core.legacy_assets import LegacyAsset
from cb_core.settings import Settings
from qa.conftest import GROUP_ID, Context, feed, make_message_update, next_update_id
from qa.mock_telegram import MockTelegram

scenarios("fun_partneredcons.feature")

_POSTER_BYTES = b"qa fake jpg bytes for a partnered convention poster"

_POSTER = LegacyAsset(
    source_path="Countdown/Patas/poster.jpg",
    destination_key="legacy/v1-bucket/qa/qa-con-poster.jpg",
    byte_size=len(_POSTER_BYTES),
    content_hash="qa-con-poster",
)


@pytest.fixture(scope="module", autouse=True)
def _poster_storage(run: Callable[[Coroutine[Any, Any, Any]], Any]) -> Iterator[None]:
    from cb_core import storage

    already_initialised = True
    try:
        storage.store()
    except RuntimeError:
        already_initialised = False

    if not already_initialised:
        run(storage.init_storage(Settings(service_name="cb-qa-cons", traces_enabled=False)))
    run(storage.store().put(_POSTER.storage_key, _POSTER_BYTES))
    yield
    if not already_initialised:
        run(storage.close_storage())


class Ctx:
    def __init__(self) -> None:
        self.poster: LegacyAsset | None = _POSTER

    def alloc_id(self) -> int:
        return next_update_id()


@pytest.fixture
def con_ctx() -> Ctx:
    return Ctx()


@pytest.fixture(autouse=True)
def _patch_pool(con_ctx: Ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(legacy_assets, "choose", lambda *_a, **_kw: con_ctx.poster)


@pytest.fixture(autouse=True)
def _restore_switches(run: Callable[[Coroutine[Any, Any, Any]], Any]) -> Iterator[None]:
    """The ungated-dispatch scenario turns both feature switches off on the
    shared QA group, and `group_config._l1` is process-global — the same leak
    guard `qa/test_fun_death.py` documents."""
    yield
    with contextlib.suppress(Exception):
        run(group_config.set_config(GROUP_ID, functions_fun=True, functions_utility=True))
    group_config._l1.clear()  # noqa: SLF001


def _photo_calls(telegram: MockTelegram) -> list[dict[str, str]]:
    return telegram.calls_to("sendPhoto")


# --------------------------------------------------------------------- given


@given("that the bot is in the group and properly set up")
def bot_set_up(ctx: Context) -> None:
    ctx.bot_running = True


@given("that the user is a member of the group")
def user_is_member() -> None:
    """Nothing to arrange — these six commands read no registry at all."""


@given("the fun feature is turned off")
def fun_disabled(database: Any, run: Callable[[Coroutine[Any, Any, Any]], Any]) -> None:
    run(group_config.set_config(GROUP_ID, functions_fun=False))
    group_config._l1.clear()  # noqa: SLF001


@given("the utility feature is turned off")
def utility_disabled(database: Any, run: Callable[[Coroutine[Any, Any, Any]], Any]) -> None:
    run(group_config.set_config(GROUP_ID, functions_utility=False))
    group_config._l1.clear()  # noqa: SLF001


@given("the poster pool is empty")
def pool_is_empty(con_ctx: Ctx) -> None:
    con_ctx.poster = None


# ---------------------------------------------------------------------- when


@when(parsers.parse('the user types the command "{command}"'))
def user_types_command(
    con_ctx: Ctx,
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
    command: str,
) -> None:
    feed(run, dispatcher, bot, make_message_update(command, con_ctx.alloc_id()))


# ---------------------------------------------------------------------- then


@then(parsers.parse('the bot should send a picture of the "{event}" convention to the group'))
def bot_sends_a_picture(telegram: MockTelegram, event: str) -> None:
    """QA asks only for a picture. Which event it depicts is decided by the
    bucket prefix the handler draws from — asserted per command in
    `packages/cb-gateway/tests/test_partneredcons.py`'s table test, since a
    mock cannot tell one JPEG from another."""
    assert _photo_calls(telegram), f"expected a sendPhoto call for {event}"


@then("the picture should carry a countdown caption naming the event")
def picture_has_a_countdown(telegram: MockTelegram) -> None:
    caption = str(_photo_calls(telegram)[-1].get("caption", ""))
    assert "Patas" in caption, caption
    assert "📆 11 a 14/12" in caption, caption


@then("the picture should carry no caption at all")
def picture_has_no_caption(telegram: MockTelegram) -> None:
    assert not str(_photo_calls(telegram)[-1].get("caption", ""))


@then("the bot should still send the picture")
def bot_still_sends(telegram: MockTelegram) -> None:
    """v1 dispatches these six above the utility check and outside the fun
    block (`COOKIEBOT.py:248-253`), so neither switch reaches them."""
    assert _photo_calls(telegram)


@then("the bot should send nothing at all")
def bot_sends_nothing(telegram: MockTelegram) -> None:
    assert not _photo_calls(telegram)
    assert not telegram.calls_to("sendMessage")
