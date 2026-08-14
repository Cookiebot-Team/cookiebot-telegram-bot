"""Unit coverage for `cb_worker.backfill.random_media` — the pointer parsing,
the kind derivation, and every outcome of one pointer's round trip.

No database and no Telegram: `_group_known`/`_already_imported` and
`storage.media` are the seams, the same way `test_youtube_job.py` fakes its own
two. The database-backed half (a real `media_objects` row, and the second run
skipping it) is `qa/integration/test_backfill_random_media.py`.
"""

from __future__ import annotations

import io
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest

from cb_worker.backfill import BackfillReport, PointerResult
from cb_worker.backfill import random_media as backfill


def _doc(**overrides: Any) -> dict[str, Any]:
    doc = {"_id": "-1001234567890", "idMessage": "42", "idMedia": "AgACAgEAAx"}
    doc.update(overrides)
    return doc


class _FakeFile:
    def __init__(self, file_path: str = "photos/file_1.jpg") -> None:
        self.file_path = file_path


def _bot(*, file_path: str = "photos/file_1.jpg", data: bytes = b"real bytes") -> AsyncMock:
    bot = AsyncMock()
    bot.get_file = AsyncMock(return_value=_FakeFile(file_path))
    # A fresh buffer per call: a single `BytesIO` is exhausted after the first
    # `.read()`, which made a multi-pointer run look like "downloaded zero
    # bytes" from the second row on.
    bot.download_file = AsyncMock(side_effect=lambda *_a, **_kw: io.BytesIO(data))
    return bot


@pytest.fixture
def known_group(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backfill, "_group_known", AsyncMock(return_value=True))
    monkeypatch.setattr(backfill, "_already_imported", AsyncMock(return_value=False))


@pytest.fixture
def fake_media(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Records what `media().put` was asked to store."""
    calls: list[dict[str, Any]] = []

    class _Ref:
        deduplicated = False
        byte_size = 10

    async def _put(group_id: int, kind: str, data: bytes, **kwargs: Any) -> _Ref:
        calls.append({"group_id": group_id, "kind": kind, "data": data, **kwargs})
        return _Ref()

    monkeypatch.setattr(
        backfill.storage, "media", lambda: type("M", (), {"put": staticmethod(_put)})
    )
    return calls


class TestParsePointer:
    def test_a_normal_document(self) -> None:
        assert backfill.parse_pointer(_doc()) == (-1001234567890, "AgACAgEAAx")

    def test_a_missing_file_id_is_unusable(self) -> None:
        assert backfill.parse_pointer(_doc(idMedia=None)) is None
        assert backfill.parse_pointer(_doc(idMedia="")) is None

    def test_an_unparseable_chat_id_is_unusable(self) -> None:
        """Every v1 id is a string; one that will not parse as an integer is
        skipped, never guessed at — `importer.mappers`' own rule."""
        assert backfill.parse_pointer(_doc(_id="not-a-number")) is None


class TestKindForPath:
    @pytest.mark.parametrize(
        ("path", "kind"),
        [
            ("photos/file_1.jpg", "photo"),
            ("photos/file_1.JPG", "photo"),
            ("videos/file_2.mp4", "video"),
            ("animations/file_3.gif", "animation"),
        ],
    )
    def test_known_suffixes(self, path: str, kind: str) -> None:
        assert backfill.kind_for_path(path) == kind

    def test_an_unknown_suffix_falls_back_rather_than_dropping_the_row(self) -> None:
        assert backfill.kind_for_path("documents/file_9.dat") == "photo"


class TestBackfillPointer:
    async def test_an_unusable_pointer_is_skipped(self) -> None:
        result = await backfill.backfill_pointer(_bot(), _doc(_id="nope"))
        assert result.outcome == "skipped"
        assert result.detail == "unusable pointer"

    async def test_an_unimported_group_is_skipped_with_the_fix(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(backfill, "_group_known", AsyncMock(return_value=False))
        result = await backfill.backfill_pointer(_bot(), _doc())
        assert result.outcome == "skipped"
        assert "import-mongo" in result.detail

    async def test_an_already_imported_pointer_never_downloads(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(backfill, "_group_known", AsyncMock(return_value=True))
        monkeypatch.setattr(backfill, "_already_imported", AsyncMock(return_value=True))
        bot = _bot()
        result = await backfill.backfill_pointer(bot, _doc())
        assert result.detail == "already imported"
        bot.get_file.assert_not_awaited()

    async def test_a_dry_run_never_downloads(self, known_group: None) -> None:
        bot = _bot()
        result = await backfill.backfill_pointer(bot, _doc(), dry_run=True)
        assert result.outcome == "skipped"
        bot.get_file.assert_not_awaited()

    async def test_a_successful_pointer_stores_the_real_bytes(
        self, known_group: None, fake_media: list[dict[str, Any]]
    ) -> None:
        result = await backfill.backfill_pointer(_bot(data=b"jpeg bytes"), _doc())
        assert result.outcome == "imported"
        assert fake_media[0]["group_id"] == -1001234567890
        assert fake_media[0]["kind"] == "photo"
        assert fake_media[0]["data"] == b"jpeg bytes"
        assert fake_media[0]["telegram_file_id"] == "AgACAgEAAx"
        assert fake_media[0]["uploaded_by"] is None
        assert fake_media[0]["sfw"] is True

    async def test_a_video_pointer_stores_the_video_kind(
        self, known_group: None, fake_media: list[dict[str, Any]]
    ) -> None:
        await backfill.backfill_pointer(_bot(file_path="videos/file_2.mp4"), _doc())
        assert fake_media[0]["kind"] == "video"

    async def test_an_expired_file_id_fails_that_row_only(self, known_group: None) -> None:
        """The expected failure for a years-old pointer: deleted message or an
        expired file id, both a 400 from `getFile`."""
        bot = _bot()
        bot.get_file = AsyncMock(
            side_effect=TelegramBadRequest(method=None, message="file is temporarily unavailable")  # type: ignore[arg-type]
        )
        result = await backfill.backfill_pointer(bot, _doc())
        assert result.outcome == "failed"
        assert "telegram" in result.detail

    async def test_an_empty_download_is_a_failure_not_a_zero_byte_row(
        self, known_group: None, fake_media: list[dict[str, Any]]
    ) -> None:
        result = await backfill.backfill_pointer(_bot(data=b""), _doc())
        assert result.outcome == "failed"
        assert not fake_media


class _FakeSource:
    def __init__(self, docs: list[dict[str, Any]], *, has_collection: bool = True) -> None:
        self._docs = docs
        self._has = has_collection

    def collections(self) -> list[str]:
        return ["randomdatabase"] if self._has else ["configs"]

    def read(self, collection: str) -> list[dict[str, Any]]:
        return self._docs

    def count(self, collection: str) -> int | None:
        return len(self._docs)

    def close(self) -> None:
        pass


class TestRunBackfill:
    async def test_counts_every_outcome(
        self, known_group: None, fake_media: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        docs = [_doc(idMedia="one"), _doc(idMedia=""), _doc(idMedia="three")]
        report = await backfill.run_backfill(_FakeSource(docs), _bot())  # type: ignore[arg-type]
        assert report.read == 3
        assert report.imported == 2
        assert report.skipped == 1

    async def test_a_missing_collection_is_a_warning_not_a_failure(self) -> None:
        report = await backfill.run_backfill(
            _FakeSource([], has_collection=False),  # type: ignore[arg-type]
            _bot(),
        )
        assert report.read == 0

    async def test_limit_stops_early(
        self, known_group: None, fake_media: list[dict[str, Any]]
    ) -> None:
        docs = [_doc(idMedia=str(index)) for index in range(5)]
        report = await backfill.run_backfill(_FakeSource(docs), _bot(), limit=2)  # type: ignore[arg-type]
        assert report.read == 2


class TestReport:
    def test_only_non_imported_rows_are_listed(self) -> None:
        report = BackfillReport()
        report.record(PointerResult(1, "a", "imported", "photo: 10 bytes"))
        report.record(PointerResult(1, "b", "failed", "telegram: gone"))
        assert report.imported == 1
        assert [r.file_id for r in report.results] == ["b"]
