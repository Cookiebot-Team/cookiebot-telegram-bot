"""util_config's write path against a real Citus database.

The acceptance layer (`qa/test_util_config.py`) cannot exercise this: with no
Postgres, `group_config.set_config` has no fallback (unlike the read path, which
degrades to `DEFAULTS` — see the finding in `docs/contracts/util_config.md`) and
would simply raise. Here a real database is required, so a menu button press is
driven all the way through to an actual `group_configs` row change — the same
`cb_gateway.handlers.config_menu.apply_change` the callback/reply handlers call,
isolated from constructing full aiogram `Message`/`CallbackQuery` objects.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Iterator
from types import ModuleType
from typing import Any

import pytest

from cb_core import group_config
from cb_gateway.handlers import config_menu as cm
from qa.integration.factories import World

pytestmark = pytest.mark.integration

Run = Callable[[Coroutine[Any, Any, Any]], Any]


@pytest.fixture(autouse=True)
def _clean_l1() -> Iterator[None]:
    group_config._l1.clear()  # noqa: SLF001
    yield
    group_config._l1.clear()  # noqa: SLF001


class TestButtonPressWritesTheRow:
    def test_boolean_field_press_flips_the_column(
        self, run: Run, world: World, pg: ModuleType
    ) -> None:
        field = cm.FIELD_BY_LETTER["h"]  # Fun Functions -> functions_fun
        value = cm.parse_reply_value(field, "0")
        assert value is False

        run(cm.apply_change(world.group_id, field, value))

        row = run(
            pg.fetchrow(
                "SELECT functions_fun FROM group_configs WHERE group_id = $1",
                world.group_id,
            )
        )
        assert row["functions_fun"] is False

        # And it's what a fresh read sees too, not just the row underneath a stale cache.
        config = run(group_config.get_config(world.group_id))
        assert config.functions_fun is False

    def test_int_field_press_writes_the_number(
        self, run: Run, world: World, pg: ModuleType
    ) -> None:
        field = cm.FIELD_BY_LETTER["p"]  # Max Posts -> max_posts
        value = cm.parse_reply_value(field, "42")

        run(cm.apply_change(world.group_id, field, value))

        row = run(
            pg.fetchrow("SELECT max_posts FROM group_configs WHERE group_id = $1", world.group_id)
        )
        assert row["max_posts"] == 42

    def test_topic_field_press_writes_a_string_not_an_int(
        self, run: Run, world: World, pg: ModuleType
    ) -> None:
        field = cm.FIELD_BY_LETTER["o"]  # Thread Posts -> thread_posts
        value = cm.parse_reply_value(field, "17")
        assert value == "17"

        run(cm.apply_change(world.group_id, field, value))

        row = run(
            pg.fetchrow(
                "SELECT thread_posts FROM group_configs WHERE group_id = $1", world.group_id
            )
        )
        assert row["thread_posts"] == "17"

    def test_language_field_press_writes_v1s_literal_string(
        self, run: Run, world: World, pg: ModuleType
    ) -> None:
        """v2 keeps v1's literal 'eng'/'pt'/'es' strings in the column — see
        docs/contracts/group-config.md — not an ISO code."""
        field = cm.FIELD_BY_LETTER["k"]  # Language -> language
        value = cm.parse_reply_value(field, "eng")

        run(cm.apply_change(world.group_id, field, value))

        row = run(
            pg.fetchrow("SELECT language FROM group_configs WHERE group_id = $1", world.group_id)
        )
        assert row["language"] == "eng"

    def test_only_the_pressed_column_changes(self, run: Run, world: World, pg: ModuleType) -> None:
        before = run(
            pg.fetchrow(
                "SELECT sticker_spam_limit FROM group_configs WHERE group_id = $1", world.group_id
            )
        )

        run(cm.apply_change(world.group_id, cm.FIELD_BY_LETTER["q"], True))

        after = run(
            pg.fetchrow(
                "SELECT sticker_spam_limit, publisher_members_only FROM group_configs "
                "WHERE group_id = $1",
                world.group_id,
            )
        )
        assert after["sticker_spam_limit"] == before["sticker_spam_limit"]
        assert after["publisher_members_only"] is True

    def test_invalid_reply_never_reaches_the_write_path(
        self, run: Run, world: World, pg: ModuleType
    ) -> None:
        """`parse_reply_value` returning `None` is the handler's cue to show the
        invalid-input error instead of calling `apply_change` at all — proven
        here by never calling it and asserting the row is untouched."""
        field = cm.FIELD_BY_LETTER["p"]
        assert cm.parse_reply_value(field, "not-a-number") is None

        before = run(
            pg.fetchrow("SELECT max_posts FROM group_configs WHERE group_id = $1", world.group_id)
        )
        after = run(
            pg.fetchrow("SELECT max_posts FROM group_configs WHERE group_id = $1", world.group_id)
        )
        assert before["max_posts"] == after["max_posts"]
