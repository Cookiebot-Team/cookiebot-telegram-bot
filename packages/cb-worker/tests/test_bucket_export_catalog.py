"""`legacy-catalog` — regrouping a bucket-export manifest into
`cb_core.legacy_assets`'s per-prefix CSV catalogs.

Two kinds of fixture, deliberately: most tests build `ManifestEntry` records
by hand and append them with `manifest.append`, the exact same
"construct the real dataclass, don't hand-write JSON" approach
`test_bucket_export.py`'s own `TestManifest`/`TestVerifyManifest` already use
— that's what lets a single test isolate one behaviour (a `"failed"` row, an
unknown prefix, a resumed run's newer verdict) without needing a real export
to produce it. `TestRealManifestSlice` is the exception: it reads a genuine
40-line slice of a real (in-progress) bucket-export manifest, committed as a
fixture, specifically to prove this module and `cb_core.legacy_assets` agree
on the real record shape, not just the shape these tests invented.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest

from cb_worker.bucket_export import ManifestEntry
from cb_worker.bucket_export import manifest as manifest_io
from cb_worker.bucket_export.catalog import (
    CSV_FIELDS,
    build_catalogs,
    main,
    render_csv,
    write_catalogs,
)

_FIXTURE_SLICE = Path(__file__).parent / "fixtures" / "bucket_export_manifest_slice.jsonl"


def _entry(
    *,
    prefix: str,
    source_path: str,
    content_hash: str = "aa6e7f88e71f1d8373a79d0a2cf1465e",
    destination_key: str | None = None,
    byte_size: int = 3,
    outcome: str = "copied",
    detail: str = "copied",
    exported_at: str = "2026-01-01T00:00:00+00:00",
) -> ManifestEntry:
    """One `ManifestEntry`, defaulted so a test only has to name the field it
    actually varies — the same shape `test_bucket_export.py`'s literal
    `ManifestEntry(...)` calls use, just de-duplicated across many tests."""
    key = destination_key
    if key is None and outcome != "failed":
        ext = "." + source_path.rsplit(".", 1)[-1] if "." in source_path else ""
        key = f"legacy/v1-bucket/{content_hash[:2]}/{content_hash}{ext}"
    return ManifestEntry(
        prefix=prefix,
        source_path=source_path,
        byte_size=byte_size,
        content_hash=content_hash if outcome != "failed" else None,
        destination_key=key if outcome != "failed" else None,
        outcome=outcome,  # type: ignore[arg-type]
        detail=detail,
        exported_at=exported_at,
    )


def _write_manifest(path: Path, entries: list[ManifestEntry]) -> None:
    for entry in entries:
        manifest_io.append(path, entry)


class TestBuildCatalogsFiltering:
    def test_only_copied_and_skipped_rows_are_emitted(self, tmp_path: Path) -> None:
        manifest_path = tmp_path / "m.jsonl"
        _write_manifest(
            manifest_path,
            [
                _entry(prefix="Death", source_path="Death/a.png", outcome="copied"),
                _entry(prefix="Death", source_path="Death/b.png", outcome="skipped"),
                _entry(prefix="Death", source_path="Death/bad.png", outcome="failed"),
            ],
        )

        groups, _, _ = build_catalogs(manifest_path)

        source_paths = {row.source_path for row in groups["Death"]}
        assert source_paths == {"Death/a.png", "Death/b.png"}

    def test_folder_placeholders_are_dropped_and_counted(self, tmp_path: Path) -> None:
        """The real export produced five of these (`Countdown/Furcamp/`,
        `Countdown/Pawstral/`, `Custom/{akiiny,dragoonie,meleys}/`) — zero-byte
        GCS folder markers that reached the catalogs on the first generation
        run and would have been drawable by `legacy_assets.choose`, i.e. a
        handler sending an empty file. See `catalog.is_folder_placeholder`.
        """
        manifest_path = tmp_path / "m.jsonl"
        _write_manifest(
            manifest_path,
            [
                _entry(
                    prefix="Countdown/Pawstral",
                    source_path="Countdown/Pawstral/",
                    byte_size=0,
                    content_hash="af1349b9f5f9a1a6a0404dea36dcc949",
                ),
                _entry(prefix="Countdown/Pawstral", source_path="Countdown/Pawstral/a.jpg"),
                _entry(
                    prefix="Custom/",
                    source_path="Custom/akiiny/",
                    byte_size=0,
                    content_hash="af1349b9f5f9a1a6a0404dea36dcc949",
                ),
                _entry(prefix="Custom/", source_path="Custom/akiiny/a.jpg"),
            ],
        )

        groups, _, placeholders = build_catalogs(manifest_path)

        assert placeholders == 2
        assert [row.source_path for row in groups["Countdown/Pawstral"]] == [
            "Countdown/Pawstral/a.jpg"
        ]
        assert [row.source_path for row in groups["Custom/akiiny"]] == ["Custom/akiiny/a.jpg"]

    def test_a_placeholder_is_the_only_row_leaves_no_catalog_at_all(self, tmp_path: Path) -> None:
        """A command folder holding nothing but its own marker produces no
        catalog file, so `custom_command_names()` never advertises a command
        with an empty pool."""
        manifest_path = tmp_path / "m.jsonl"
        _write_manifest(
            manifest_path,
            [_entry(prefix="Custom/", source_path="Custom/ghost/", byte_size=0)],
        )

        groups, _, placeholders = build_catalogs(manifest_path)

        assert groups == {}
        assert placeholders == 1

    def test_latest_by_source_wins_over_an_older_failed_row(self, tmp_path: Path) -> None:
        """A resumed run can turn an earlier `"failed"` verdict into a later
        `"copied"` one for the same source path — only the newer line must
        reach the catalog."""
        manifest_path = tmp_path / "m.jsonl"
        _write_manifest(
            manifest_path,
            [
                _entry(
                    prefix="Death",
                    source_path="Death/a.png",
                    outcome="failed",
                    detail="transient error",
                    exported_at="2026-01-01T00:00:00+00:00",
                ),
                _entry(
                    prefix="Death",
                    source_path="Death/a.png",
                    outcome="copied",
                    exported_at="2026-01-02T00:00:00+00:00",
                ),
            ],
        )

        groups, _, _ = build_catalogs(manifest_path)

        assert [row.source_path for row in groups["Death"]] == ["Death/a.png"]

    def test_latest_by_source_wins_the_other_direction_too(self, tmp_path: Path) -> None:
        """A blob present in one run and content-deleted in a later one (the
        later line records `"failed"`) must not leave a stale `"copied"` row
        behind."""
        manifest_path = tmp_path / "m.jsonl"
        _write_manifest(
            manifest_path,
            [
                _entry(
                    prefix="Death",
                    source_path="Death/a.png",
                    outcome="copied",
                    exported_at="2026-01-01T00:00:00+00:00",
                ),
                _entry(
                    prefix="Death",
                    source_path="Death/a.png",
                    outcome="failed",
                    detail="gone",
                    exported_at="2026-01-02T00:00:00+00:00",
                ),
            ],
        )

        groups, _, _ = build_catalogs(manifest_path)

        assert groups.get("Death", []) == []


class TestCustomGrouping:
    def test_custom_rows_are_grouped_by_command_name_not_by_the_bare_prefix(
        self, tmp_path: Path
    ) -> None:
        manifest_path = tmp_path / "m.jsonl"
        _write_manifest(
            manifest_path,
            [
                _entry(prefix="Custom/", source_path="Custom/emoji/1.png"),
                _entry(prefix="Custom/", source_path="Custom/emoji/2.png"),
                _entry(prefix="Custom/", source_path="Custom/skull/1.png"),
            ],
        )

        groups, unknown, _ = build_catalogs(manifest_path)

        assert unknown == ()  # "Custom/" itself is a known PREFIXES entry
        assert {row.source_path for row in groups["Custom/emoji"]} == {
            "Custom/emoji/1.png",
            "Custom/emoji/2.png",
        }
        assert [row.source_path for row in groups["Custom/skull"]] == ["Custom/skull/1.png"]

    def test_custom_entry_with_no_command_segment_raises(self, tmp_path: Path) -> None:
        """`"Custom"` with no trailing slash and no sub-folder: not a folder
        placeholder (`is_folder_placeholder` keys on the trailing `/`), so it
        still reaches `_catalog_key` and is still a hard error rather than a
        row filed under some invented command name. The `"Custom/"` spelling
        of the same malformed row is now dropped one step earlier, as the
        folder marker it is.
        """
        manifest_path = tmp_path / "m.jsonl"
        _write_manifest(manifest_path, [_entry(prefix="Custom/", source_path="Custom")])

        with pytest.raises(ValueError, match="no command sub-folder"):
            build_catalogs(manifest_path)


class TestUnknownPrefixes:
    def test_unknown_prefix_is_reported_not_dropped(self, tmp_path: Path) -> None:
        manifest_path = tmp_path / "m.jsonl"
        _write_manifest(
            manifest_path,
            [_entry(prefix="Mystery", source_path="Mystery/a.png")],
        )

        groups, unknown, _ = build_catalogs(manifest_path)

        assert unknown == ("Mystery",)
        # Reported *and* still catalogued — nothing exported is thrown away
        # just because PREFIXES has not been told about this folder.
        assert [row.source_path for row in groups["Mystery"]] == ["Mystery/a.png"]

    def test_known_prefixes_are_never_flagged(self, tmp_path: Path) -> None:
        manifest_path = tmp_path / "m.jsonl"
        _write_manifest(
            manifest_path,
            [_entry(prefix="Death", source_path="Death/a.png")],
        )

        _, unknown, _ = build_catalogs(manifest_path)

        assert unknown == ()


class TestDeterministicOutput:
    def test_row_order_is_by_source_path_regardless_of_manifest_order(self, tmp_path: Path) -> None:
        manifest_path = tmp_path / "m.jsonl"
        _write_manifest(
            manifest_path,
            [
                _entry(prefix="Death", source_path="Death/z.png"),
                _entry(prefix="Death", source_path="Death/a.png"),
                _entry(prefix="Death", source_path="Death/m.png"),
            ],
        )

        groups, _, _ = build_catalogs(manifest_path)

        assert [row.source_path for row in groups["Death"]] == [
            "Death/a.png",
            "Death/m.png",
            "Death/z.png",
        ]

    def test_two_builds_from_the_same_manifest_are_byte_identical(self, tmp_path: Path) -> None:
        manifest_path = tmp_path / "m.jsonl"
        _write_manifest(
            manifest_path,
            [
                _entry(prefix="Death", source_path="Death/z.png"),
                _entry(prefix="Death", source_path="Death/a.png"),
                _entry(prefix="Countdown/BFF", source_path="Countdown/BFF/x.png"),
            ],
        )

        out_a = tmp_path / "out_a"
        out_b = tmp_path / "out_b"
        write_catalogs(manifest_path, out_a)
        write_catalogs(manifest_path, out_b)

        assert (out_a / "death.csv").read_bytes() == (out_b / "death.csv").read_bytes()
        assert (out_a / "countdown" / "bff.csv").read_bytes() == (
            out_b / "countdown" / "bff.csv"
        ).read_bytes()


class TestCatalogRelpathNesting:
    def test_flat_prefix(self, tmp_path: Path) -> None:
        manifest_path = tmp_path / "m.jsonl"
        _write_manifest(manifest_path, [_entry(prefix="Death", source_path="Death/a.png")])
        report = write_catalogs(manifest_path, tmp_path / "out")
        assert report.catalogs["Death"].relpath == "death.csv"
        assert (tmp_path / "out" / "death.csv").is_file()

    def test_countdown_nesting(self, tmp_path: Path) -> None:
        manifest_path = tmp_path / "m.jsonl"
        _write_manifest(
            manifest_path, [_entry(prefix="Countdown/BFF", source_path="Countdown/BFF/a.png")]
        )
        report = write_catalogs(manifest_path, tmp_path / "out")
        assert report.catalogs["Countdown/BFF"].relpath == "countdown/bff.csv"
        assert (tmp_path / "out" / "countdown" / "bff.csv").is_file()

    def test_custom_nesting(self, tmp_path: Path) -> None:
        manifest_path = tmp_path / "m.jsonl"
        _write_manifest(manifest_path, [_entry(prefix="Custom/", source_path="Custom/Emoji/a.png")])
        report = write_catalogs(manifest_path, tmp_path / "out")
        assert report.catalogs["Custom/Emoji"].relpath == "custom/emoji.csv"
        assert (tmp_path / "out" / "custom" / "emoji.csv").is_file()


class TestWriteCatalogsDryRun:
    def test_dry_run_writes_nothing(self, tmp_path: Path) -> None:
        manifest_path = tmp_path / "m.jsonl"
        _write_manifest(manifest_path, [_entry(prefix="Death", source_path="Death/a.png")])
        output_root = tmp_path / "out"

        report = write_catalogs(manifest_path, output_root, dry_run=True)

        assert report.catalogs["Death"].rows == 1
        assert not output_root.exists()

    def test_dry_run_and_real_run_report_the_same_counts(self, tmp_path: Path) -> None:
        manifest_path = tmp_path / "m.jsonl"
        _write_manifest(
            manifest_path,
            [
                _entry(prefix="Death", source_path="Death/a.png"),
                _entry(prefix="Death", source_path="Death/b.png"),
            ],
        )

        dry = write_catalogs(manifest_path, tmp_path / "dry_out", dry_run=True)
        real = write_catalogs(manifest_path, tmp_path / "real_out", dry_run=False)

        assert dry.total_rows() == real.total_rows() == 2


class TestRenderCsv:
    def test_header_and_rows_round_trip_through_csv_dictreader(self, tmp_path: Path) -> None:
        manifest_path = tmp_path / "m.jsonl"
        _write_manifest(
            manifest_path,
            [_entry(prefix="Death", source_path="Death/a.png", byte_size=42)],
        )
        groups, _, _ = build_catalogs(manifest_path)
        text = render_csv(groups["Death"])

        reader = csv.DictReader(io.StringIO(text))
        assert reader.fieldnames == list(CSV_FIELDS)
        rows = list(reader)
        assert rows[0]["source_path"] == "Death/a.png"
        assert rows[0]["byte_size"] == "42"

    def test_uses_unix_line_endings(self) -> None:
        text = render_csv([])
        assert "\r\n" not in text


class TestRealManifestSlice:
    """A genuine 40-line slice of a real (in-progress) bucket-export
    manifest — proves this module parses the real record shape, not a shape
    these tests invented for their own convenience."""

    def test_real_slice_produces_one_catalog_with_every_row(self) -> None:
        groups, unknown, _ = build_catalogs(_FIXTURE_SLICE)

        assert unknown == ()  # "IdeiaDesenho" is in PREFIXES
        assert set(groups) == {"IdeiaDesenho"}
        assert len(groups["IdeiaDesenho"]) == 40
        first = next(r for r in groups["IdeiaDesenho"] if r.source_path == "IdeiaDesenho/10003.png")
        assert first.content_hash == "aa6e7f88e71f1d8373a79d0a2cf1465e"
        assert first.destination_key == "legacy/v1-bucket/aa/aa6e7f88e71f1d8373a79d0a2cf1465e.png"
        assert first.byte_size == 810154


class TestCli:
    def test_help_exits_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as excinfo:
            main(["--help"])
        assert excinfo.value.code == 0

    def test_missing_manifest_reports_a_clear_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["--manifest", str(tmp_path / "nope.jsonl"), "--output", str(tmp_path / "out")])
        assert code == 2
        assert "no manifest" in capsys.readouterr().err

    def test_dry_run_via_cli_writes_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        manifest_path = tmp_path / "m.jsonl"
        _write_manifest(manifest_path, [_entry(prefix="Death", source_path="Death/a.png")])
        output_root = tmp_path / "out"

        code = main(
            [
                "--manifest",
                str(manifest_path),
                "--output",
                str(output_root),
                "--dry-run",
            ]
        )

        assert code == 0
        assert not output_root.exists()
        out = capsys.readouterr().out
        assert "dry run" in out
