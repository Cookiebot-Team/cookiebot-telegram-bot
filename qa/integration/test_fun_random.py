"""fun_random against a real Citus database.

Exercises the two DB seams `cb_gateway.handlers.fun_random` owns:

- the write side (`pool_media` / `_pool` / `_should_pool`), against a real
  `group_configs` row (`COOKIEBOT.py:168-172`'s `sfw and funfunctions and not
  publisherpost` gate, plus the forwarded/NSFW-title guards), proving the pure
  predicate agrees with what `cb_core.group_config` actually returns for a
  seeded group — not just a hand-built fake;
- the read side (`_select_media`), against a real `media_objects` row,
  including the `sfw_only=ctx.config.sfw` filter this port adds on top of v1
  (module docstring: `_should_pool` gates the *write*, `_select_media` applies
  the same flag again on *read*).

`Bot.download` is faked rather than routed through `qa/mock_telegram.py`
(which does not implement `getFile` — see `qa/test_fun_random.py`'s own
docstring for why): AGENTS.md §6 allows mocking the outside world, and a
Telegram file download is exactly that.
"""

from __future__ import annotations

import io
from collections.abc import Callable, Coroutine
from types import ModuleType
from typing import Any

import pytest
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.types import Message

from cb_core import group_config
from cb_gateway.context import ChatContext
from cb_gateway.handlers import fun_random
from qa.integration.factories import World

pytestmark = pytest.mark.integration

Run = Callable[[Coroutine[Any, Any, Any]], Any]


@pytest.fixture(scope="module", autouse=True)
def _media_storage(run: Run) -> Any:
    """A real `MediaService` over an in-memory blob store — same defensive
    init/close pattern as `qa/test_fun_random.py`, so a session that already
    brought storage up for another feature is left alone."""
    from cb_core import storage
    from cb_core.settings import Settings

    already_initialised = True
    try:
        storage.media()
    except RuntimeError:
        already_initialised = False

    if not already_initialised:
        run(
            storage.init_storage(
                Settings(service_name="cb-integration-fun-random", traces_enabled=False)
            )
        )
    yield
    if not already_initialised:
        run(storage.close_storage())


@pytest.fixture(autouse=True)
def _clean_l1() -> Any:
    group_config._l1.clear()  # noqa: SLF001
    yield
    group_config._l1.clear()  # noqa: SLF001


class _FakeBot:
    """Fakes the one thing v1 never needed and this port adds: downloading the
    actual bytes behind a `file_id` (module docstring's "re-architecture"
    section). This is the outside world (Telegram), not our own code."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self.downloaded: list[str] = []

    async def download(self, file_id: str) -> io.BytesIO:
        self.downloaded.append(file_id)
        return io.BytesIO(self._payload)


def _photo_message(
    *, group_id: int, chat_title: str, user_id: int, file_id: str, forwarded: bool = False
) -> Message:
    payload: dict[str, Any] = {
        "message_id": 1,
        "date": 0,
        "chat": {"id": group_id, "type": "supergroup", "title": chat_title},
        "from": {"id": user_id, "is_bot": False, "first_name": "Tester"},
        "photo": [
            {
                "file_id": file_id,
                "file_unique_id": f"u-{file_id}",
                "width": 90,
                "height": 90,
                "file_size": 1,
            }
        ],
    }
    if forwarded:
        payload["forward_origin"] = {
            "type": "user",
            "date": 0,
            "sender_user": {"id": 999, "is_bot": False, "first_name": "Original"},
        }
    return Message.model_validate(payload)


class TestShouldPoolAgainstRealConfig:
    """The pure predicate, fed a `GroupConfig` this port did not hand-build —
    proving `_should_pool` agrees with what `group_config.get_config` actually
    returns for a seeded row, not just a fake with the right attribute names."""

    def test_stock_config_pools(self, run: Run, world: World) -> None:
        config = run(group_config.get_config(world.group_id))
        assert fun_random._should_pool(  # noqa: SLF001
            config, chat_title="Clean Group", forwarded=False
        )

    def test_sfw_off_refuses(self, run: Run, world: World) -> None:
        world.set_config(sfw=False)
        config = run(group_config.get_config(world.group_id))
        assert not fun_random._should_pool(  # noqa: SLF001
            config, chat_title="Clean Group", forwarded=False
        )

    def test_functions_fun_off_refuses(self, run: Run, world: World) -> None:
        world.set_config(functions_fun=False)
        config = run(group_config.get_config(world.group_id))
        assert not fun_random._should_pool(  # noqa: SLF001
            config, chat_title="Clean Group", forwarded=False
        )

    def test_publisher_post_on_refuses(self, run: Run, world: World) -> None:
        world.set_config(publisher_post=True)
        config = run(group_config.get_config(world.group_id))
        assert not fun_random._should_pool(  # noqa: SLF001
            config, chat_title="Clean Group", forwarded=False
        )


class TestPoolMediaEndToEnd:
    """`pool_media`, driven with a real (validated) aiogram `Message` and a
    real `group_configs` row, writing to a real `media_objects` row."""

    def test_a_qualifying_photo_is_written(self, run: Run, world: World) -> None:
        bot = _FakeBot(b"jpeg bytes")
        message = _photo_message(
            group_id=world.group_id, chat_title="Clean Group", user_id=1, file_id="tg-1"
        )

        with pytest.raises(SkipHandler):
            run(fun_random.pool_media(message, bot))

        assert world.count("media_objects") == 1
        assert bot.downloaded == ["tg-1"]

    def test_sfw_off_never_downloads_or_writes(self, run: Run, world: World) -> None:
        world.set_config(sfw=False)
        bot = _FakeBot(b"jpeg bytes")
        message = _photo_message(
            group_id=world.group_id, chat_title="Clean Group", user_id=1, file_id="tg-2"
        )

        with pytest.raises(SkipHandler):
            run(fun_random.pool_media(message, bot))

        assert world.count("media_objects") == 0
        assert bot.downloaded == []

    def test_forwarded_photo_is_never_written(self, run: Run, world: World) -> None:
        bot = _FakeBot(b"jpeg bytes")
        message = _photo_message(
            group_id=world.group_id,
            chat_title="Clean Group",
            user_id=1,
            file_id="tg-3",
            forwarded=True,
        )

        with pytest.raises(SkipHandler):
            run(fun_random.pool_media(message, bot))

        assert world.count("media_objects") == 0

    def test_nsfw_titled_group_is_never_written(self, run: Run, world: World) -> None:
        bot = _FakeBot(b"jpeg bytes")
        message = _photo_message(
            group_id=world.group_id, chat_title="NSFW Group", user_id=1, file_id="tg-4"
        )

        with pytest.raises(SkipHandler):
            run(fun_random.pool_media(message, bot))

        assert world.count("media_objects") == 0

    def test_written_row_is_marked_sfw(self, run: Run, world: World, pg: ModuleType) -> None:
        bot = _FakeBot(b"jpeg bytes")
        message = _photo_message(
            group_id=world.group_id, chat_title="Clean Group", user_id=1, file_id="tg-5"
        )

        with pytest.raises(SkipHandler):
            run(fun_random.pool_media(message, bot))

        row = run(
            pg.fetchrow(
                "SELECT kind, sfw, telegram_file_id FROM media_objects WHERE group_id = $1",
                world.group_id,
                name="test_read_pooled_media",
            )
        )
        assert row is not None
        assert row["kind"] == "photo"
        assert row["sfw"] is True
        assert row["telegram_file_id"] == "tg-5"


class TestSelectMediaHonoursSfw:
    """`_select_media`'s `sfw_only=ctx.config.sfw` — the flag applies again on
    read (module docstring), against a real `media_objects` row this time."""

    def _ctx(self, run: Run, world: World) -> ChatContext:
        from cb_core.admins import ActorCheck

        config = run(group_config.get_config(world.group_id))
        return ChatContext(
            group_id=world.group_id,
            config=config,
            lang="en",
            actor=ActorCheck(user_id=1, is_admin=False, anonymous=False),
        )

    def test_finds_the_only_seeded_item(self, run: Run, world: World) -> None:
        from cb_core import storage

        run(storage.media().put(world.group_id, "photo", b"one photo", telegram_file_id="tg-1"))

        ref = run(fun_random._select_media(self._ctx(run, world)))  # noqa: SLF001
        assert ref is not None
        assert ref.telegram_file_id == "tg-1"

    def test_sfw_group_never_gets_the_unsafe_item(self, run: Run, world: World) -> None:
        from cb_core import storage

        run(
            storage.media().put(
                world.group_id, "photo", b"unsafe photo", telegram_file_id="unsafe", sfw=False
            )
        )
        world.set_config(sfw=True)

        ref = run(fun_random._select_media(self._ctx(run, world)))  # noqa: SLF001
        assert ref is None

    def test_a_not_sfw_group_can_still_get_the_unsafe_item(self, run: Run, world: World) -> None:
        from cb_core import storage

        run(
            storage.media().put(
                world.group_id, "photo", b"unsafe photo", telegram_file_id="unsafe", sfw=False
            )
        )
        world.set_config(sfw=False)

        ref = run(fun_random._select_media(self._ctx(run, world)))  # noqa: SLF001
        assert ref is not None
        assert ref.telegram_file_id == "unsafe"

    def test_empty_pool_returns_none(self, run: Run, world: World) -> None:
        ref = run(fun_random._select_media(self._ctx(run, world)))  # noqa: SLF001
        assert ref is None
