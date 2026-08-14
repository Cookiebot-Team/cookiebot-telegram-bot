"""The `randomdatabase` backfill against a real Citus database.

The unit tests fake both database seams; this exercises what they cannot —
that a backfilled pointer produces a genuine `media_objects` row for the right
group, that `/random`'s own read finds it afterwards, and that a second run
skips it *without downloading anything*, which is the property the whole
resume story rests on.

`Bot.get_file`/`download_file` are faked for the same reason
`qa/integration/test_fun_random.py` fakes `Bot.download`: a Telegram download
is the outside world (AGENTS.md §6), and the mock Telegram harness does not
implement `getFile`.
"""

from __future__ import annotations

import io
from collections.abc import Callable, Coroutine
from typing import Any
from unittest.mock import AsyncMock

import pytest

from cb_core import db, storage
from cb_worker.backfill import random_media as backfill
from qa.integration.factories import World

pytestmark = pytest.mark.integration

Run = Callable[[Coroutine[Any, Any, Any]], Any]


@pytest.fixture(scope="module", autouse=True)
def _media_storage(run: Run) -> Any:
    from cb_core.settings import Settings

    already_initialised = True
    try:
        storage.media()
    except RuntimeError:
        already_initialised = False

    if not already_initialised:
        run(
            storage.init_storage(
                Settings(service_name="cb-integration-backfill", traces_enabled=False)
            )
        )
    yield
    if not already_initialised:
        run(storage.close_storage())


class _FakeFile:
    def __init__(self, file_path: str) -> None:
        self.file_path = file_path


def _bot(file_path: str = "photos/file_1.jpg", data: bytes = b"backfilled jpeg bytes") -> AsyncMock:
    bot = AsyncMock()
    bot.get_file = AsyncMock(return_value=_FakeFile(file_path))
    bot.download_file = AsyncMock(side_effect=lambda *_a, **_kw: io.BytesIO(data))
    return bot


def _doc(group_id: int, file_id: str) -> dict[str, Any]:
    """One `randomdatabase` document, in v1's own shape: every id a string
    (`RandomDatabase.java`)."""
    return {"_id": str(group_id), "idMessage": "9001", "idMedia": file_id}


class TestBackfillWritesARealRow:
    def test_a_pointer_becomes_a_media_object_for_its_group(self, run: Run, world: World) -> None:
        bot = _bot()

        result = run(backfill.backfill_pointer(bot, _doc(world.group_id, "file-one")))

        assert result.outcome == "imported", result.detail
        row = run(
            db.fetchrow(
                "SELECT kind, byte_size, telegram_file_id, uploaded_by, sfw "
                "FROM media_objects WHERE group_id = $1 AND telegram_file_id = $2",
                world.group_id,
                "file-one",
                name="test_backfill_row",
            )
        )
        assert row is not None
        assert row["kind"] == "photo"
        assert row["byte_size"] == len(b"backfilled jpeg bytes")
        assert row["uploaded_by"] is None
        assert row["sfw"] is True

    def test_the_row_is_indistinguishable_from_a_pooled_one(self, run: Run, world: World) -> None:
        """`/random`'s own read has to find it — that is the whole point of
        the backfill, and it is why this writes through
        `storage.media().put` rather than an INSERT of its own."""
        run(backfill.backfill_pointer(_bot(), _doc(world.group_id, "file-two")))

        ref = run(storage.media().random(world.group_id, kinds=("photo", "video"), sfw_only=True))

        assert ref is not None
        assert ref.telegram_file_id == "file-two"

    def test_a_second_run_skips_without_downloading(self, run: Run, world: World) -> None:
        first = _bot()
        run(backfill.backfill_pointer(first, _doc(world.group_id, "file-three")))

        second = _bot()
        result = run(backfill.backfill_pointer(second, _doc(world.group_id, "file-three")))

        assert result.outcome == "skipped"
        assert result.detail == "already imported"
        second.get_file.assert_not_awaited()

    def test_a_video_pointer_stores_the_video_kind(self, run: Run, world: World) -> None:
        run(
            backfill.backfill_pointer(
                _bot(file_path="videos/file_7.mp4", data=b"mp4 bytes"),
                _doc(world.group_id, "file-four"),
            )
        )
        row = run(
            db.fetchrow(
                "SELECT kind FROM media_objects WHERE group_id = $1 AND telegram_file_id = $2",
                world.group_id,
                "file-four",
                name="test_backfill_kind",
            )
        )
        assert row is not None
        assert row["kind"] == "video"

    def test_an_unimported_group_is_skipped_not_written(self, run: Run) -> None:
        """`media_objects.group_id` is a foreign key: without this check the
        insert would fail the whole run instead of reporting one row that
        needs `import-mongo` to go first."""
        missing_group = -1_00_999_999_999
        bot = _bot()

        result = run(backfill.backfill_pointer(bot, _doc(missing_group, "file-five")))

        assert result.outcome == "skipped"
        assert "import-mongo" in result.detail
        bot.get_file.assert_not_awaited()

    def test_two_groups_pointing_at_identical_bytes_each_get_a_row(
        self, run: Run, world: World, second_world: World
    ) -> None:
        """Dedupe is per tenant: the blob is written once, but each group keeps
        its own reference (`0002_media_and_llm_usage.py`'s `UNIQUE (group_id,
        content_hash)`)."""
        first, second = world, second_world
        same_bytes = b"identical bytes in two groups"

        run(backfill.backfill_pointer(_bot(data=same_bytes), _doc(first.group_id, "file-a")))
        run(backfill.backfill_pointer(_bot(data=same_bytes), _doc(second.group_id, "file-b")))

        rows = run(
            db.fetch(
                "SELECT group_id, blob_key FROM media_objects WHERE group_id = ANY($1::bigint[])",
                [first.group_id, second.group_id],
                name="test_backfill_dedupe",
            )
        )
        assert len(rows) == 2
        assert rows[0]["blob_key"] == rows[1]["blob_key"]
