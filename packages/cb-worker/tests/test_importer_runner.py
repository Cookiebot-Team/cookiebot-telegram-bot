"""Unit tests for `cb_worker.importer.runner.run_import`'s progress hooks.

Everything else about `run_import` (mapper dispatch, table write order, the
FK-stub safety net) is proven against a real database by
`qa/integration/test_importer.py`; what belongs here, with no Postgres and no
MongoDB anywhere in the loop, is the contract `cb_worker.cutover` depends on:
`on_collection_start`/`on_collection_done` fire exactly once per collection,
in read order, and leave `run_import`'s own behaviour byte-for-byte unchanged
when they are left at their default of `None` (module docstring, "no rich
dependency").

`dry_run=True` throughout: it exercises the same mapper/report path a real run
does (`_import_collection`'s docstring) without ever calling `cb_core.db`, so
these tests need no database connection at all.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence

import pytest

from cb_worker.importer import Document, MappedRows
from cb_worker.importer.runner import MAPPERS, run_import


class FakeMongoSource:
    """In-memory `MongoSource` — no network, no database, per this module's
    own docstring."""

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


def _configs_doc(chat_id: str) -> Document:
    # Only the fields `mappers.map_configs` actually reads; a document missing
    # v1's other keys is exactly what a partially-migrated group looks like.
    return {
        "_id": chat_id,
        "furbots": True,
        "stickerSpamLimit": "5",
        "timeWithoutSendingImages": 0,
        "timeCaptcha": 60,
        "functionsFun": True,
        "functionsUtility": True,
        "sfw": False,
        "language": "en",
        "publisherPost": False,
        "publisherAsk": False,
        "publisherMembersOnly": False,
        "threadPosts": "0",
        "maxPosts": 0,
    }


def _groups_doc(group_id: str) -> Document:
    return {"groupId": group_id, "name": "Test Group", "imageUrl": None, "adminUsers": []}


class TestProgressCallbacks:
    async def test_fires_once_per_collection_in_read_order(self) -> None:
        source = FakeMongoSource(
            {
                "configs": [_configs_doc("1"), _configs_doc("2")],
                "groups": [_groups_doc("1")],
            }
        )
        started: list[str] = []
        done: list[tuple[str, int, int]] = []

        report = await run_import(
            source,
            dry_run=True,
            on_collection_start=started.append,
            on_collection_done=lambda name, read, written: done.append((name, read, written)),
        )

        # _COLLECTION_ORDER puts "groups" before "configs" regardless of dict
        # insertion order (runner.py's own module docstring, point 1).
        assert started == ["groups", "configs"]
        assert [name for name, _, _ in done] == ["groups", "configs"]

        groups_read, groups_written = next((r, w) for name, r, w in done if name == "groups")
        assert groups_read == 1
        assert groups_written == 1  # one groups row

        configs_read, configs_written = next((r, w) for name, r, w in done if name == "configs")
        assert configs_read == 2
        assert configs_written == 2  # one group_configs row per document

        # A hook fires as many times as the collection loop runs, not once per
        # row — the row/write counts are carried in the callback's arguments.
        assert report.read == {"groups": 1, "configs": 2}

    async def test_done_fires_even_when_a_collection_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _raising_mapper(document: Document, mapped: MappedRows) -> None:
            raise RuntimeError("simulated mapper crash")

        monkeypatch.setitem(MAPPERS, "boom", _raising_mapper)
        source = FakeMongoSource({"boom": [{"_id": "1"}]})
        done: list[tuple[str, int, int]] = []

        report = await run_import(
            source,
            dry_run=True,
            on_collection_done=lambda name, read, written: done.append((name, read, written)),
        )

        # The collection is recorded as a whole failure (per run_import's own
        # docstring), but the progress hook still fires exactly once for it —
        # a cutover progress bar must still reach 100%, not hang on the one
        # collection that blew up.
        assert done == [("boom", 0, 0)]
        assert any(s.collection == "boom" and s.document_id == "*" for s in report.skipped)

    async def test_default_none_hooks_leave_behaviour_unchanged(self) -> None:
        source = FakeMongoSource({"groups": [_groups_doc("1")]})

        report = await run_import(source, dry_run=True)

        assert report.read == {"groups": 1}
        assert report.written == {"groups": 1}
