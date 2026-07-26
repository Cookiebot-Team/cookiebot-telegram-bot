"""Admin resolution against a real Citus and a real Valkey.

Telegram is still faked here (the outside world) — only the persistence and
cache plumbing are real. These tests exercise the part unit tests cannot: that
`refresh()` actually writes `group_admins` rows scoped to `group_id`, and that a
second refresh replaces them rather than piling up duplicates.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Iterator
from types import ModuleType
from typing import TYPE_CHECKING, Any

import pytest
from aiogram.types import ChatMemberAdministrator, ChatMemberOwner, User

from cb_core import admins as admins_mod
from cb_core import cache
from cb_core.settings import Settings

if TYPE_CHECKING:
    from qa.integration.factories import World

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module", autouse=True)
def _cache(run: Callable[[Coroutine[Any, Any, Any]], Any]) -> Iterator[None]:
    """Admin caching writes through Valkey; init it once for this module.

    Mirrors the `pg` fixture's "skip cleanly if unreachable" contract instead of
    failing the whole module when Valkey isn't up.
    """
    settings = Settings(service_name="cb-tests-admins", traces_enabled=False)
    try:
        run(cache.init_cache(settings))
    except Exception as exc:  # noqa: BLE001 - any connection failure means "skip"
        pytest.skip(f"no valkey at {settings.redis_dsn}: {exc}")
    yield
    run(cache.close_cache())


class FakeBot:
    """Stands in for aiogram's `Bot.get_chat_administrators`."""

    def __init__(self, members: list[Any] | None = None, *, error: Exception | None = None) -> None:
        self.members = members or []
        self.error = error
        self.calls: list[int] = []

    async def get_chat_administrators(self, chat_id: int) -> list[Any]:
        self.calls.append(chat_id)
        if self.error is not None:
            raise self.error
        return self.members


def _owner(user_id: int) -> ChatMemberOwner:
    return ChatMemberOwner(
        status="creator",
        user=User(id=user_id, is_bot=False, first_name="Owner"),
        is_anonymous=False,
    )


def _administrator(user_id: int, *, can_restrict_members: bool = True) -> ChatMemberAdministrator:
    return ChatMemberAdministrator(
        status="administrator",
        user=User(id=user_id, is_bot=False, first_name=f"Admin{user_id}"),
        can_be_edited=False,
        is_anonymous=False,
        can_manage_chat=True,
        can_delete_messages=True,
        can_manage_video_chats=True,
        can_restrict_members=can_restrict_members,
        can_promote_members=False,
        can_change_info=True,
        can_invite_users=True,
        can_post_stories=False,
        can_edit_stories=False,
        can_delete_stories=False,
    )


class TestRefreshPersistence:
    def test_refresh_writes_expected_rows(
        self, run: Callable[[Coroutine[Any, Any, Any]], Any], world: World, pg: ModuleType
    ) -> None:
        owner = world.add_user()
        deputy = world.add_user()
        bot = FakeBot([_owner(owner.user_id), _administrator(deputy.user_id)])

        result = run(admins_mod.refresh(bot, world.group_id))

        assert {a.user_id for a in result} == {owner.user_id, deputy.user_id}
        assert world.count("group_admins") == 2

        rows = run(
            pg.fetch(
                "SELECT user_id, role FROM group_admins WHERE group_id = $1 ORDER BY user_id",
                world.group_id,
            )
        )
        by_id = {r["user_id"]: r["role"] for r in rows}
        assert by_id == {owner.user_id: "creator", deputy.user_id: "administrator"}

    def test_second_refresh_replaces_not_duplicates(
        self, run: Callable[[Coroutine[Any, Any, Any]], Any], world: World, pg: ModuleType
    ) -> None:
        first_admin = world.add_user()
        second_admin = world.add_user()

        bot1 = FakeBot([_owner(first_admin.user_id)])
        run(admins_mod.refresh(bot1, world.group_id))
        assert world.count("group_admins") == 1

        # A membership change: the old admin steps down, a new one is promoted.
        bot2 = FakeBot([_administrator(second_admin.user_id)])
        run(admins_mod.refresh(bot2, world.group_id))

        assert world.count("group_admins") == 1, "replace, not accumulate"
        rows = run(
            pg.fetch(
                "SELECT user_id, role FROM group_admins WHERE group_id = $1",
                world.group_id,
            )
        )
        assert [r["user_id"] for r in rows] == [second_admin.user_id]

    def test_read_filters_on_group_id(
        self, run: Callable[[Coroutine[Any, Any, Any]], Any], world: World, pg: ModuleType
    ) -> None:
        from qa.integration.factories import World

        other = World(run)
        other.setup()
        try:
            mine = world.add_user()
            theirs = other.add_user()

            run(admins_mod.refresh(FakeBot([_owner(mine.user_id)]), world.group_id))
            run(admins_mod.refresh(FakeBot([_owner(theirs.user_id)]), other.group_id))

            mine_ids = run(admins_mod.admin_ids(FakeBot([_owner(mine.user_id)]), world.group_id))
            theirs_ids = run(
                admins_mod.admin_ids(FakeBot([_owner(theirs.user_id)]), other.group_id)
            )

            assert mine_ids == {mine.user_id}
            assert theirs_ids == {theirs.user_id}
            assert world.count("group_admins") == 1
            assert other.count("group_admins") == 1
        finally:
            other.teardown()

    def test_is_admin_reflects_persisted_and_cached_state(
        self, run: Callable[[Coroutine[Any, Any, Any]], Any], world: World
    ) -> None:
        admin_user = world.add_user()
        plain_user = world.add_user()
        bot = FakeBot([_owner(admin_user.user_id)])

        run(admins_mod.refresh(bot, world.group_id))

        assert run(admins_mod.is_admin(bot, world.group_id, admin_user.user_id)) is True
        assert run(admins_mod.is_admin(bot, world.group_id, plain_user.user_id)) is False
