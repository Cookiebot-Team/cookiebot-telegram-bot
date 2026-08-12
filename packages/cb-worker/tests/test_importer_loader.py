"""Unit tests for the importer's loader/runner layer — no database.

`loader.load_rows` and `loader.ensure_group_stubs` are driven through
`cb_core.db.executemany`/`execute`, monkeypatched to record calls rather than
touch a socket — the same seam-substitution pattern
`packages/cb-core/tests/test_group_config.py` uses for its own DB calls.

The dry-run test drives `runner.run_import` end to end against a hand-rolled fake
`MongoSource` and hand-rolled mapper functions (never the real `mappers.py`,
which a concurrent agent is building against the same `importer/__init__.py`
contract): `_load_runner()` stubs `cb_worker.importer.mappers` in `sys.modules`
if it is not on disk yet, purely so importing `runner` (which imports `MAPPERS`
at module scope) does not fail while that file is still being written. Once the
real module exists this stub is never installed and `runner` imports normally.
"""

from __future__ import annotations

import importlib
import sys
import types
from collections.abc import Iterator, Sequence
from typing import Any

import pytest

import cb_worker.importer.loader as loader
from cb_worker.importer import Document, MappedRows

EXPECTED_TABLES = {
    "groups",
    "group_configs",
    "group_rules",
    "group_welcomes",
    "group_admins",
    "users",
    "blacklist",
    "sticker_pool",
}

#: Tables distributed on group_id — every conflict key on these must carry it
#: (Citus rule, AGENTS.md §4.3).
DISTRIBUTED_TABLES = {"groups", "group_configs", "group_rules", "group_welcomes", "group_admins"}


# --------------------------------------------------------------------- TABLE_LOADS


def test_table_loads_covers_every_table_the_mappers_target() -> None:
    assert set(loader.TABLE_LOADS) == EXPECTED_TABLES


@pytest.mark.parametrize("table", sorted(EXPECTED_TABLES))
def test_conflict_columns_are_a_subset_of_columns(table: str) -> None:
    load = loader.TABLE_LOADS[table]
    assert set(load.conflict_columns) <= set(load.columns)


@pytest.mark.parametrize("table", sorted(EXPECTED_TABLES))
def test_update_columns_are_a_subset_of_columns_and_never_the_key(table: str) -> None:
    load = loader.TABLE_LOADS[table]
    assert set(load.update_columns) <= set(load.columns)
    # The natural key is never reassigned by its own upsert.
    assert not (set(load.update_columns) & set(load.conflict_columns))


@pytest.mark.parametrize("table", sorted(DISTRIBUTED_TABLES))
def test_distributed_tables_key_on_group_id(table: str) -> None:
    assert "group_id" in loader.TABLE_LOADS[table].conflict_columns


def test_reference_tables_key_on_their_own_primary_key() -> None:
    assert loader.TABLE_LOADS["users"].conflict_columns == ("user_id",)
    assert loader.TABLE_LOADS["blacklist"].conflict_columns == ("subject_id",)
    assert loader.TABLE_LOADS["sticker_pool"].conflict_columns == ("file_id",)


def test_sticker_pool_has_nothing_left_to_reassert_on_conflict() -> None:
    """`file_id` is both the only column and the key — a re-run is `DO
    NOTHING`, never a `DO UPDATE`, because there is no other field a re-run
    could legitimately overwrite."""
    load = loader.TABLE_LOADS["sticker_pool"]
    assert load.columns == ("file_id",)
    assert load.update_columns == ()
    assert "DO NOTHING" in loader._upsert_sql(load)  # noqa: SLF001


def test_group_configs_never_reasserts_its_two_v2_only_columns() -> None:
    """sticker_spam_window_s/doomlist_enabled get a value on first insert (the
    mapper supplies v1's true default for both, since v1 has no such field at
    all) but a re-run must never reassert it — the import has no genuine v1
    signal for either, so it must not undo a value the bot changed since."""
    load = loader.TABLE_LOADS["group_configs"]
    assert "sticker_spam_window_s" in load.columns
    assert "doomlist_enabled" in load.columns
    assert "sticker_spam_window_s" not in load.update_columns
    assert "doomlist_enabled" not in load.update_columns


def test_stamped_tables_refresh_their_timestamp_on_every_conflict() -> None:
    """updated_at/synced_at are import bookkeeping, not user data — reasserting
    them on every re-run is exactly right (see loader.py's module docstring)."""
    for table, stamp in loader._STAMP_COLUMN.items():  # noqa: SLF001
        load = loader.TABLE_LOADS[table]
        assert stamp in load.columns
        assert stamp in load.update_columns


def test_first_seen_timestamps_are_never_in_columns_at_all() -> None:
    """groups.joined_at, users.created_at, blacklist.created_at: "when we first
    saw this" is Postgres's own DEFAULT now(), fired once, never reasserted."""
    assert "joined_at" not in loader.TABLE_LOADS["groups"].columns
    assert "left_at" not in loader.TABLE_LOADS["groups"].columns
    assert "created_at" not in loader.TABLE_LOADS["users"].columns
    assert "created_at" not in loader.TABLE_LOADS["blacklist"].columns


def test_groups_leaves_lifecycle_columns_untouched() -> None:
    """chat_type/skin/joined_at/left_at are v2-owned; overwriting left_at would
    resurrect a group the gateway already recorded as departed."""
    load = loader.TABLE_LOADS["groups"]
    for column in ("chat_type", "skin", "joined_at", "left_at", "username"):
        assert column not in load.update_columns


def test_upsert_sql_uses_do_nothing_when_no_update_columns() -> None:
    sql = loader._upsert_sql(loader.TABLE_LOADS["blacklist"])  # noqa: SLF001
    assert "DO NOTHING" in sql
    assert "DO UPDATE" not in sql


def test_upsert_sql_uses_do_update_set_for_writable_columns() -> None:
    sql = loader._upsert_sql(loader.TABLE_LOADS["group_configs"])  # noqa: SLF001
    assert "DO UPDATE SET" in sql
    assert "language = EXCLUDED.language" in sql
    assert "group_id = EXCLUDED.group_id" not in sql


# ------------------------------------------------------------------- batching maths


@pytest.mark.parametrize(
    ("total_rows", "batch_size", "expected_batch_sizes"),
    [
        (0, 500, []),
        (1, 500, [1]),
        (500, 500, [500]),
        (501, 500, [500, 1]),
        (1200, 500, [500, 500, 200]),
        (7, 3, [3, 3, 1]),
        (3, 3, [3]),
    ],
)
async def test_load_rows_batches_by_batch_size(
    monkeypatch: pytest.MonkeyPatch,
    total_rows: int,
    batch_size: int,
    expected_batch_sizes: list[int],
) -> None:
    calls: list[list[tuple[Any, ...]]] = []

    async def fake_executemany(
        stmt: str, rows: Sequence[Sequence[Any]], *, name: str = "executemany"
    ) -> None:
        calls.append([tuple(r) for r in rows])

    monkeypatch.setattr(loader.db, "executemany", fake_executemany)

    # blacklist carries no _STAMP_COLUMN, so load_rows passes rows through
    # unmodified — a 1-tuple stand-in is enough to test batch partitioning in
    # isolation from any particular table's real column count.
    rows = [(i,) for i in range(total_rows)]
    written = await loader.load_rows("blacklist", rows, batch_size=batch_size)

    assert written == total_rows
    assert [len(batch) for batch in calls] == expected_batch_sizes
    # Batches partition the input in order, nothing dropped or duplicated.
    assert [row for batch in calls for row in batch] == rows


async def test_load_rows_appends_one_stamp_value_shared_by_every_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """group_rules carries a _STAMP_COLUMN (updated_at): a mapper's 2-tuple
    (group_id, body) gains a third, loader-supplied timestamp, and every row
    across every batch of this one call shares the identical instant — never a
    fresh now() per row or per batch."""
    calls: list[list[tuple[Any, ...]]] = []

    async def fake_executemany(
        stmt: str, rows: Sequence[Sequence[Any]], *, name: str = "executemany"
    ) -> None:
        calls.append([tuple(r) for r in rows])

    monkeypatch.setattr(loader.db, "executemany", fake_executemany)

    rows = [(1, "rule one"), (2, "rule two")]
    written = await loader.load_rows("group_rules", rows, batch_size=1)  # forces 2 batches

    assert written == 2
    assert len(calls) == 2  # batch_size=1 split them into separate executemany calls
    written_rows = [row for batch in calls for row in batch]
    assert all(len(row) == 3 for row in written_rows)  # (group_id, body, stamp)
    assert {row[-1] for row in written_rows} == {written_rows[0][-1]}  # one shared instant


async def test_load_rows_empty_input_never_calls_executemany(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _boom(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("executemany must not be called for zero rows")

    monkeypatch.setattr(loader.db, "executemany", _boom)

    written = await loader.load_rows("blacklist", [], batch_size=500)

    assert written == 0


async def test_load_rows_rejects_a_table_with_no_registered_load() -> None:
    with pytest.raises(ValueError, match="no TableLoad"):
        await loader.load_rows("not_a_real_table", [(1,)], batch_size=10)


# ----------------------------------------------------------------- ensure_group_stubs


async def test_ensure_group_stubs_dedupes_and_sorts_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fake_execute(stmt: str, *args: Any, **kwargs: Any) -> str:
        calls.append((stmt, args))
        return "INSERT 0 3"

    monkeypatch.setattr(loader.db, "execute", fake_execute)

    written = await loader.ensure_group_stubs([5, 3, 5, 3, 9])

    assert written == 3
    stmt, args = calls[0]
    assert "ON CONFLICT (group_id) DO NOTHING" in stmt
    assert args[0] == [3, 5, 9]


async def test_ensure_group_stubs_empty_input_never_touches_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _boom(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("execute must not be called for zero ids")

    monkeypatch.setattr(loader.db, "execute", _boom)

    written = await loader.ensure_group_stubs([])

    assert written == 0


# --------------------------------------------------------------------- dry-run runner


class FakeMongoSource:
    """Hand-rolled `MongoSource`: an in-memory dict of collection -> documents."""

    def __init__(self, docs: dict[str, list[Document]]) -> None:
        self._docs = docs

    def collections(self) -> Sequence[str]:
        return list(self._docs)

    def read(self, collection: str) -> Iterator[Document]:
        yield from self._docs.get(collection, [])

    def count(self, collection: str) -> int | None:
        return len(self._docs.get(collection, []))

    def close(self) -> None:
        pass


def _map_groups(doc: Document, mapped: MappedRows) -> None:
    mapped.add("groups", (doc["group_id"], doc["title"], doc["image_url"]))
    for admin in doc["admins"]:
        mapped.add("group_admins", (doc["group_id"], admin, "administrator", False))


def _map_configs(doc: Document, mapped: MappedRows) -> None:
    mapped.add(
        "group_configs",
        (
            doc["group_id"],
            True,  # allow_furbots
            5,  # sticker_spam_limit
            60,  # sticker_spam_window_s (constant, no v1 source)
            600,  # media_restrict_seconds
            300,  # captcha_timeout_seconds
            True,  # functions_fun
            True,  # functions_utility
            True,  # sfw
            "en",  # language
            False,  # publisher_post
            True,  # publisher_ask
            False,  # publisher_members_only
            None,  # thread_posts
            9999,  # max_posts
            True,  # doomlist_enabled (constant, no v1 source)
        ),
    )


def _load_runner() -> types.ModuleType:
    """Import `cb_worker.importer.runner`, stubbing `mappers` if not on disk yet.

    `runner.py` imports `MAPPERS` from `cb_worker.importer.mappers` at module
    scope; that file is owned by a concurrent agent and may not exist when this
    suite runs. The stub is only installed when the real module is genuinely
    missing, and the test below replaces `runner.MAPPERS` with its own fakes
    regardless, so this never masks a real integration problem once the real
    file lands — it only keeps this file collectible in the meantime.
    """
    if "cb_worker.importer.mappers" not in sys.modules:
        try:
            importlib.import_module("cb_worker.importer.mappers")
        except ModuleNotFoundError:
            stub = types.ModuleType("cb_worker.importer.mappers")
            stub.MAPPERS = {}  # type: ignore[attr-defined]
            sys.modules["cb_worker.importer.mappers"] = stub
    return importlib.import_module("cb_worker.importer.runner")


async def test_dry_run_reports_counts_and_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _load_runner()
    monkeypatch.setattr(runner, "MAPPERS", {"groups": _map_groups, "configs": _map_configs})

    async def _boom_execute(*args: Any, **kwargs: Any) -> str:
        raise AssertionError("dry run must not call db.execute")

    async def _boom_executemany(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("dry run must not call db.executemany")

    monkeypatch.setattr(loader.db, "execute", _boom_execute)
    monkeypatch.setattr(loader.db, "executemany", _boom_executemany)

    source = FakeMongoSource(
        {
            "groups": [{"group_id": 1, "title": "G1", "image_url": None, "admins": [10, 20]}],
            "configs": [{"group_id": 2}],
        }
    )

    report = await runner.run_import(source, dry_run=True, batch_size=500)

    assert report.read == {"groups": 1, "configs": 1}
    assert report.written == {"groups": 1, "group_admins": 2, "group_configs": 1}
    assert report.skipped == []
