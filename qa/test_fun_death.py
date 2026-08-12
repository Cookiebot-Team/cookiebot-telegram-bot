"""Step definitions for fun_death.

QA: qa/features/fun_death.feature (synced from Cookiebot-QA/features/fun_death.feature
for the first two scenarios; see that file's own header for what was added).
Contract: docs/contracts/fun_death.md.

This checkout has not run `cb.py legacy-catalog` (spec.md's "The blocker" — now
resolved as an *infrastructure* gap, but the catalog itself is a generated
artefact nobody has built in this checkout), so every scenario here
monkeypatches `cb_core.legacy_assets.choose` to a small fake pool and seeds
matching bytes into `cb_core.storage` directly. `legacy_assets.choose`'s real
implementation is a CSV read — not what's under test here,
`cb_gateway.handlers.death` is — the same "mock the outside world, not our
own code" boundary `qa/test_fun_random.py` draws around `MediaService`
(AGENTS.md §6).
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

scenarios("fun_death.feature")

_GIF_BYTES = b"qa fake gif bytes for fun_death"
_PHOTO_BYTES = b"qa fake jpg bytes for fun_death"

# Fake catalog rows standing in for a real `legacy-catalog` run (module
# docstring) — one `.gif`, one still image, so the gif/photo dispatch
# scenario has something real to pick between.
_GIF_ENTRY = LegacyAsset(
    source_path="Death/skull.gif",
    destination_key="legacy/v1-bucket/qa/qa-death-gif.gif",
    byte_size=len(_GIF_BYTES),
    content_hash="qa-death-gif",
)
_PHOTO_ENTRY = LegacyAsset(
    source_path="Death/skull.jpg",
    destination_key="legacy/v1-bucket/qa/qa-death-photo.jpg",
    byte_size=len(_PHOTO_BYTES),
    content_hash="qa-death-photo",
)

# The message a "reply to another user" scenario replies to — a different
# sender than the QA harness's default `_user()`, so branch (2)'s "replied-to
# first_name" is distinguishable from branch (3)'s "caller's own name".
_REPLIED_TO_MESSAGE: dict[str, Any] = {
    "message_id": 9001,
    "date": 0,
    "chat": {"id": GROUP_ID, "type": "supergroup", "title": "QA Group"},
    "from": {"id": 555999, "is_bot": False, "first_name": "Replier", "username": "replier"},
    "text": "hello",
}


# ------------------------------------------------------------------ fixtures


@pytest.fixture(scope="module", autouse=True)
def _death_storage(run: Callable[[Coroutine[Any, Any, Any]], Any]) -> Iterator[None]:
    """A real blob store over `memory://`, seeded with both pool entries'
    bytes — the same defensive "only init/close if nothing else already did"
    shape `qa/test_fun_random.py`'s `_media_storage` uses, since several
    suites in one session share a process-wide store."""
    from cb_core import storage

    already_initialised = True
    try:
        storage.store()
    except RuntimeError:
        already_initialised = False

    if not already_initialised:
        run(storage.init_storage(Settings(service_name="cb-qa-fun-death", traces_enabled=False)))
    run(storage.store().put(_GIF_ENTRY.storage_key, _GIF_BYTES))
    run(storage.store().put(_PHOTO_ENTRY.storage_key, _PHOTO_BYTES))
    yield
    if not already_initialised:
        run(storage.close_storage())


class Ctx:
    """Per-scenario state on top of qa/conftest.py's base `Context`."""

    def __init__(self) -> None:
        self.pool_entry: LegacyAsset | None = _GIF_ENTRY

    def alloc_id(self) -> int:
        # Shared process-wide counter: a per-scenario counter collides with
        # earlier scenarios and the dedupe middleware drops the update.
        return next_update_id()


@pytest.fixture
def death_ctx() -> Ctx:
    return Ctx()


@pytest.fixture(autouse=True)
def _patch_pool(death_ctx: Ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    """`legacy_assets.choose` is the one thing this suite fakes (module
    docstring); everything downstream of it — the handler, the real blob
    store — runs for real."""
    monkeypatch.setattr(legacy_assets, "choose", lambda *_a, **_kw: death_ctx.pool_entry)


@pytest.fixture(autouse=True)
def _reset_config(database: Any, run: Callable[[Coroutine[Any, Any, Any]], Any]) -> Iterator[None]:
    """Same leak guard `qa/test_x_unearth.py`/`qa/test_fun_random.py` apply:
    the fun-off scenario flips `functions_fun` on the shared QA group, and
    `group_config._l1` is process-global.

    It takes `database` for the *ordering*, not for the connection. pytest
    finalises fixtures in reverse setup order, so without that dependency the
    `database` fixture closes the pool first and the restore below raises into
    the `suppress` — leaving the flag off for whatever suite runs next. That is
    not hypothetical: it is how `qa/test_fun_ship.py` started failing with "Fun
    functions are disabled" the first time this suite ran alongside it.
    """
    yield
    with contextlib.suppress(Exception):
        run(group_config.set_config(GROUP_ID, functions_fun=True))
    group_config._l1.clear()  # noqa: SLF001


def _last_caption(telegram: MockTelegram) -> str:
    calls = telegram.calls_to("sendAnimation") + telegram.calls_to("sendPhoto")
    assert calls, f"expected a sendAnimation or sendPhoto call, got {telegram.calls}"
    return str(calls[-1].get("caption", ""))


# --------------------------------------------------------------------- given


@given("that the bot is in the group and properly set up")
def bot_set_up(ctx: Context) -> None:
    ctx.bot_running = True


@given("that the user is in the group")
def user_in_group() -> None:
    pass


@given("that fun functions are disabled for the group")
def fun_disabled(database: Any, run: Callable[[Coroutine[Any, Any, Any]], Any]) -> None:
    """Takes the `database` fixture for the same reason `unearth`'s does: the
    flag lives in `group_configs`, and that fixture skips the scenario
    cleanly when no Postgres is listening rather than failing it."""
    run(group_config.set_config(GROUP_ID, functions_fun=False))


@given("that the death pool's chosen entry is a still image")
def pool_is_still_image(death_ctx: Ctx) -> None:
    death_ctx.pool_entry = _PHOTO_ENTRY


@given("that the death asset pool is empty")
def pool_is_empty(death_ctx: Ctx) -> None:
    """D-DE-3's real-not-hypothetical state (module docstring): a catalog
    `legacy-catalog` has never generated in this deployment."""
    death_ctx.pool_entry = None


# ---------------------------------------------------------------------- when


@when("the user sends the command /death")
def user_sends_death(
    death_ctx: Ctx, run: Callable[[Coroutine[Any, Any, Any]], Any], dispatcher: Dispatcher, bot: Bot
) -> None:
    feed(run, dispatcher, bot, make_message_update("/death", death_ctx.alloc_id()))


@when(parsers.parse('the user sends the command "{command}"'))
def user_sends_named_command(
    death_ctx: Ctx,
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
    command: str,
) -> None:
    feed(run, dispatcher, bot, make_message_update(command, death_ctx.alloc_id()))


@when("the user sends the command /death and tags another user")
def user_tags_another(
    death_ctx: Ctx, run: Callable[[Coroutine[Any, Any, Any]], Any], dispatcher: Dispatcher, bot: Bot
) -> None:
    # v1 reads the raw second token verbatim, no membership lookup
    # (spec.md's target-resolution branch (1)) — "@tagged" need not be a real
    # registered member for this to work, and that is the point being tested.
    feed(run, dispatcher, bot, make_message_update("/death @tagged", death_ctx.alloc_id()))


@when("the user sends the command /death as a reply to another user's message")
def user_replies(
    death_ctx: Ctx, run: Callable[[Coroutine[Any, Any, Any]], Any], dispatcher: Dispatcher, bot: Bot
) -> None:
    feed(
        run,
        dispatcher,
        bot,
        make_message_update("/death", death_ctx.alloc_id(), reply_to=_REPLIED_TO_MESSAGE),
    )


# ---------------------------------------------------------------------- then


@then("the bot should reply with a meme and a random skull gif")
def bot_sends_meme(telegram: MockTelegram) -> None:
    _last_caption(telegram)  # asserts a sendAnimation or sendPhoto call exists


@then("random cause of death for the user")
def cause_of_death_for_user(telegram: MockTelegram) -> None:
    # qa/conftest.py's default sender: `_user()` -> username "tester", so
    # branch (3) (no tag, no reply) renders "@tester" with the skull prefix.
    caption = _last_caption(telegram)
    assert caption.startswith("💀💀💀 @tester"), caption


@then("random cause of death for the tagged user")
def cause_of_death_for_tagged(telegram: MockTelegram) -> None:
    caption = _last_caption(telegram)
    assert caption.startswith("💀💀💀 @tagged"), caption


@then("the bot replies that fun functions are off")
def bot_says_fun_off(telegram: MockTelegram) -> None:
    sent = telegram.calls_to("sendMessage")
    assert sent, "expected a sendMessage call, got none"
    assert sent[-1].get("text", "") == locales.get("fun_off", "en")


@then("random cause of death for the replied-to user")
def cause_of_death_for_replied(telegram: MockTelegram) -> None:
    # Branch (2): the replied-to sender's first name, never their username
    # (spec.md's target-resolution row) — "Replier", not "@replier".
    caption = _last_caption(telegram)
    assert caption.startswith("💀💀💀 Replier"), caption


@then("the bot sends a photo, not an animation")
def bot_sends_photo_not_animation(telegram: MockTelegram) -> None:
    assert telegram.calls_to("sendPhoto"), f"expected a sendPhoto call, got {telegram.calls}"
    assert not telegram.calls_to("sendAnimation"), telegram.calls


@then("the user receives no response")
def no_response(telegram: MockTelegram) -> None:
    assert not telegram.calls_to("sendMessage")
    assert not telegram.calls_to("sendPhoto")
    assert not telegram.calls_to("sendAnimation")
