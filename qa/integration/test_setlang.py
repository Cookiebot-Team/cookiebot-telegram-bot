"""core_setlang's write path against a real Citus database.

`qa/test_core_setlang.py` already exercises `apply_join_language` end to end
through the mock Telegram API, against a real database too (this feature's
whole point is a `group_configs` row landing, so the acceptance layer already
needs one). This module isolates the same write from Telegram entirely, driving
`cb_gateway.handlers.setlang.apply_join_language` directly with a lightweight
fake bot double (the outside world, not our own code — AGENTS.md §6), the same
shape `qa/integration/test_config_menu.py` uses for `config_menu.apply_change`.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Iterator
from types import ModuleType
from typing import Any

import pytest
from aiogram.types import BotCommand, BotCommandScopeChat

from cb_core import group_config
from cb_gateway.handlers import setlang
from qa.integration.factories import World

pytestmark = pytest.mark.integration

Run = Callable[[Coroutine[Any, Any, Any]], Any]


@pytest.fixture(autouse=True)
def _clean_l1() -> Iterator[None]:
    group_config._l1.clear()  # noqa: SLF001
    yield
    group_config._l1.clear()  # noqa: SLF001


class _FakeBot:
    """Records `setMyCommands`/`sendMessage` calls without touching a real or
    mocked Telegram server — this test is about the database write, not the
    Telegram side effect."""

    def __init__(self) -> None:
        self.set_my_commands_calls: list[dict[str, object]] = []
        self.sent_messages: list[dict[str, object]] = []

    async def set_my_commands(
        self,
        commands: list[BotCommand],
        scope: BotCommandScopeChat | None = None,
        language_code: str | None = None,
    ) -> bool:
        self.set_my_commands_calls.append(
            {"commands": commands, "scope": scope, "language_code": language_code}
        )
        return True

    async def send_message(self, chat_id: int, text: str, **kwargs: object) -> None:
        self.sent_messages.append({"chat_id": chat_id, "text": text})


class TestApplyJoinLanguageLandsInGroupConfigs:
    def test_a_brand_new_group_with_no_row_gets_the_derived_language(
        self, run: Run, world: World, pg: ModuleType
    ) -> None:
        """`World.setup()` seeds a `group_configs` row with defaults (see
        `qa/integration/factories.py`) — delete it first so this proves the
        write for a group that, like v1's first-contact case, has no config
        row at all yet."""
        run(pg.execute("DELETE FROM group_configs WHERE group_id = $1", world.group_id))
        before = run(
            pg.fetchrow("SELECT language FROM group_configs WHERE group_id = $1", world.group_id)
        )
        assert before is None

        bot = _FakeBot()
        result = run(setlang.apply_join_language(bot, world.group_id, "pt-BR"))

        assert result == "pt"
        row = run(
            pg.fetchrow("SELECT language FROM group_configs WHERE group_id = $1", world.group_id)
        )
        assert row is not None
        assert row["language"] == "pt"

        # And it's what a fresh read sees too, not just the row underneath a stale cache.
        config = run(group_config.get_config(world.group_id))
        assert config.language == "pt"

    def test_es_derivation_lands_the_literal_es_string(
        self, run: Run, world: World, pg: ModuleType
    ) -> None:
        bot = _FakeBot()
        result = run(setlang.apply_join_language(bot, world.group_id, "es-419"))
        assert result == "es"
        row = run(
            pg.fetchrow("SELECT language FROM group_configs WHERE group_id = $1", world.group_id)
        )
        assert row["language"] == "es"

    def test_english_derivation_lands_v1s_literal_eng_not_en(
        self, run: Run, world: World, pg: ModuleType
    ) -> None:
        bot = _FakeBot()
        result = run(setlang.apply_join_language(bot, world.group_id, "en-GB"))
        assert result == "eng"
        row = run(
            pg.fetchrow("SELECT language FROM group_configs WHERE group_id = $1", world.group_id)
        )
        assert row["language"] == "eng"

    def test_no_language_code_writes_nothing(self, run: Run, world: World, pg: ModuleType) -> None:
        before = run(
            pg.fetchrow("SELECT language FROM group_configs WHERE group_id = $1", world.group_id)
        )
        bot = _FakeBot()

        result = run(setlang.apply_join_language(bot, world.group_id, None))

        assert result is None
        after = run(
            pg.fetchrow("SELECT language FROM group_configs WHERE group_id = $1", world.group_id)
        )
        assert after["language"] == before["language"]
        assert bot.set_my_commands_calls == []

    def test_command_menu_relabeling_happens_alongside_the_write(
        self, run: Run, world: World, pg: ModuleType
    ) -> None:
        bot = _FakeBot()
        run(setlang.apply_join_language(bot, world.group_id, "pt-BR"))

        codes = {call["language_code"] for call in bot.set_my_commands_calls}
        assert codes == {"pt", "es", "en"}
        for call in bot.set_my_commands_calls:
            assert call["scope"].chat_id == world.group_id

    def test_only_the_language_column_changes(self, run: Run, world: World, pg: ModuleType) -> None:
        before = run(
            pg.fetchrow(
                "SELECT sticker_spam_limit, max_posts FROM group_configs WHERE group_id = $1",
                world.group_id,
            )
        )
        bot = _FakeBot()

        run(setlang.apply_join_language(bot, world.group_id, "pt-BR"))

        after = run(
            pg.fetchrow(
                "SELECT sticker_spam_limit, max_posts FROM group_configs WHERE group_id = $1",
                world.group_id,
            )
        )
        assert after["sticker_spam_limit"] == before["sticker_spam_limit"]
        assert after["max_posts"] == before["max_posts"]
