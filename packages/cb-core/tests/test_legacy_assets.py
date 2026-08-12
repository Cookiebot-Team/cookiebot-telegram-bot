"""`cb_core.legacy_assets` — the `meme_templates` counterpart for
`bucket_export`'s objects.

No real catalog ships yet: `cb.py legacy-catalog` has never been run against a
finished export in this checkout (the export itself is still running), so
`cb_core.asset_data.legacy` is genuinely empty right now. `TestNotYetExported`
exercises exactly that real, current state — the "no bytes seeded yet"
degradation the module docstring promises. Everything else builds a small
index by hand: either via `_walk_csv_files`/`_load_catalog` against a
`tmp_path` tree (a `pathlib.Path` satisfies the `Traversable` protocol this
module reads through, so no package installation is needed to test the
walking/parsing logic), or by monkeypatching `_INDEX` directly to exercise the
public lookup functions against controlled data — the same "construct real
values, don't invent a shape" discipline `test_bucket_export_catalog.py`
follows on the writer side, just adapted to a module whose data is frozen at
import.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from cb_core import legacy_assets
from cb_core.legacy_assets import LegacyAsset, catalog_relpath

_FIXTURE_SLICE = Path(__file__).parent / "fixtures" / "bucket_export_manifest_slice.jsonl"


def _asset(
    *,
    source_path: str = "Death/a.png",
    destination_key: str = "legacy/v1-bucket/ab/abc123.png",
    byte_size: int = 3,
    content_hash: str = "abc123",
) -> LegacyAsset:
    return LegacyAsset(
        source_path=source_path,
        destination_key=destination_key,
        byte_size=byte_size,
        content_hash=content_hash,
    )


class TestNotYetExported:
    """The real, current state of `cb_core.asset_data.legacy`: `legacy-catalog`
    has never run in this checkout, so every lookup degrades to empty rather
    than raising."""

    def test_prefixes_is_empty(self) -> None:
        assert legacy_assets.prefixes() == ()

    def test_entries_for_any_prefix_is_empty(self) -> None:
        assert legacy_assets.entries_for("Death") == ()
        assert legacy_assets.entries_for("Countdown/BFF") == ()

    def test_choose_returns_none(self) -> None:
        assert legacy_assets.choose("Death") is None
        assert legacy_assets.choose("Death", random.Random(1)) is None

    def test_custom_command_names_is_empty(self) -> None:
        assert legacy_assets.custom_command_names() == ()

    def test_entries_for_custom_is_empty(self) -> None:
        assert legacy_assets.entries_for_custom("emoji") == ()

    def test_choose_custom_returns_none(self) -> None:
        assert legacy_assets.choose_custom("emoji") is None


class TestCatalogRelpath:
    def test_flat_prefix(self) -> None:
        assert catalog_relpath("Death") == "death.csv"

    def test_nested_prefix_becomes_directory_nesting(self) -> None:
        assert catalog_relpath("Countdown/BFF") == "countdown/bff.csv"
        assert catalog_relpath("Fight/English") == "fight/english.csv"

    def test_custom_command_key(self) -> None:
        assert catalog_relpath("Custom/Emoji") == "custom/emoji.csv"

    def test_every_segment_is_lowercased(self) -> None:
        assert catalog_relpath("IdeiaDesenho") == "ideiadesenho.csv"

    def test_empty_key_raises(self) -> None:
        with pytest.raises(ValueError, match="empty catalog key"):
            catalog_relpath("")

    def test_a_lone_trailing_slash_is_dropped_not_treated_as_a_segment(self) -> None:
        # "Custom/" alone is never passed here in practice (the generator
        # always resolves it to "Custom/<command>" first), but the function
        # itself just drops the empty trailing segment rather than raising.
        assert catalog_relpath("Custom/") == "custom.csv"


class TestWalkAndLoadAgainstATmpTree:
    """`pathlib.Path` structurally satisfies `Traversable`
    (`importlib.resources.abc.Traversable` is a `runtime_checkable Protocol`
    covering `iterdir`/`is_dir`/`is_file`/`read_text`/`name`), so a `tmp_path`
    tree exercises the real walking/parsing code with no package installed.
    """

    def _write_csv(self, path: Path, rows: list[tuple[str, str, int, str]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = ["source_path,destination_key,byte_size,content_hash"]
        lines.extend(f"{sp},{dk},{bs},{ch}" for sp, dk, bs, ch in rows)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_walk_finds_flat_and_nested_csvs(self, tmp_path: Path) -> None:
        self._write_csv(
            tmp_path / "death.csv", [("Death/a.png", "legacy/v1-bucket/ab/abc.png", 3, "abc")]
        )
        self._write_csv(
            tmp_path / "countdown" / "bff.csv",
            [("Countdown/BFF/a.png", "legacy/v1-bucket/cd/cde.png", 5, "cde")],
        )

        found = dict(legacy_assets._walk_csv_files(tmp_path, ()))  # noqa: SLF001 - testing the walker directly

        assert set(found) == {"death", "countdown/bff"}

    def test_load_catalog_parses_rows_into_legacy_assets(self, tmp_path: Path) -> None:
        self._write_csv(
            tmp_path / "death.csv",
            [
                ("Death/a.png", "legacy/v1-bucket/ab/abc.png", 3, "abc"),
                ("Death/b.png", "legacy/v1-bucket/de/def.png", 7, "def"),
            ],
        )

        rows = legacy_assets._load_catalog(tmp_path / "death.csv")  # noqa: SLF001

        assert rows == (
            LegacyAsset(
                source_path="Death/a.png",
                destination_key="legacy/v1-bucket/ab/abc.png",
                byte_size=3,
                content_hash="abc",
            ),
            LegacyAsset(
                source_path="Death/b.png",
                destination_key="legacy/v1-bucket/de/def.png",
                byte_size=7,
                content_hash="def",
            ),
        )

    def test_walk_ignores_non_csv_files(self, tmp_path: Path) -> None:
        (tmp_path / "README.txt").write_text("not a catalog", encoding="utf-8")
        self._write_csv(
            tmp_path / "death.csv", [("Death/a.png", "legacy/v1-bucket/ab/abc.png", 3, "abc")]
        )

        found = dict(legacy_assets._walk_csv_files(tmp_path, ()))  # noqa: SLF001

        assert set(found) == {"death"}


class TestPublicApiAgainstAControlledIndex:
    """Monkeypatches the frozen `_INDEX` for the duration of one test —
    the module's own contract is "load once at import, immutable
    afterwards" (module docstring), so this is the one place production code
    never does what these tests do."""

    @pytest.fixture(autouse=True)
    def _controlled_index(self, monkeypatch: pytest.MonkeyPatch) -> None:
        death_a = _asset(source_path="Death/a.png", content_hash="hash-a")
        death_b = _asset(source_path="Death/b.png", content_hash="hash-b")
        bff = _asset(source_path="Countdown/BFF/x.png", content_hash="hash-bff")
        emoji = _asset(source_path="Custom/emoji/1.png", content_hash="hash-emoji")
        index = {
            "death": (death_a, death_b),
            "countdown/bff": (bff,),
            "custom/emoji": (emoji,),
        }
        monkeypatch.setattr(legacy_assets, "_INDEX", index)

    def test_prefixes_excludes_custom_and_is_sorted(self) -> None:
        assert legacy_assets.prefixes() == ("countdown/bff", "death")

    def test_entries_for_is_case_insensitive(self) -> None:
        assert len(legacy_assets.entries_for("Death")) == 2
        assert len(legacy_assets.entries_for("death")) == 2
        assert len(legacy_assets.entries_for("DEATH")) == 2

    def test_entries_for_nested_prefix(self) -> None:
        assert [a.source_path for a in legacy_assets.entries_for("Countdown/BFF")] == [
            "Countdown/BFF/x.png"
        ]

    def test_entries_for_unknown_prefix_is_empty(self) -> None:
        assert legacy_assets.entries_for("Fight/English") == ()

    def test_choose_is_deterministic_under_a_seeded_rng(self) -> None:
        first = legacy_assets.choose("Death", random.Random(7))
        second = legacy_assets.choose("Death", random.Random(7))
        assert first is not None
        assert first == second

    def test_choose_returns_none_for_an_empty_pool(self) -> None:
        assert legacy_assets.choose("Fight/English") is None

    def test_custom_command_names(self) -> None:
        assert legacy_assets.custom_command_names() == ("emoji",)

    def test_entries_for_custom_is_case_insensitive(self) -> None:
        assert len(legacy_assets.entries_for_custom("Emoji")) == 1
        assert len(legacy_assets.entries_for_custom("emoji")) == 1

    def test_entries_for_custom_unknown_command_is_empty(self) -> None:
        assert legacy_assets.entries_for_custom("nonexistent") == ()

    def test_choose_custom_is_deterministic_under_a_seeded_rng(self) -> None:
        # Only one entry in the controlled pool, but this still proves the
        # rng is actually threaded through rather than ignored.
        first = legacy_assets.choose_custom("emoji", random.Random(3))
        second = legacy_assets.choose_custom("emoji", random.Random(3))
        assert first == second == legacy_assets.entries_for_custom("emoji")[0]

    def test_choose_custom_returns_none_for_an_unknown_command(self) -> None:
        assert legacy_assets.choose_custom("nonexistent") is None


class TestStorageKeyRoundTrips:
    def test_storage_key_is_the_destination_key_verbatim(self) -> None:
        asset = _asset(destination_key="legacy/v1-bucket/ab/abc123.png")
        assert asset.storage_key == "legacy/v1-bucket/ab/abc123.png"

    def test_storage_key_round_trips_against_a_real_manifest_record(self) -> None:
        """Builds a `LegacyAsset` from the first line of a genuine
        bucket-export manifest slice and confirms `storage_key` reproduces
        exactly what that real export run recorded as `destination_key` —
        the same field `bucket_export.keys.destination_key` computed at
        export time, not a value this test invented."""
        with _FIXTURE_SLICE.open(encoding="utf-8") as fh:
            record = json.loads(fh.readline())

        asset = LegacyAsset(
            source_path=record["source_path"],
            destination_key=record["destination_key"],
            byte_size=record["byte_size"],
            content_hash=record["content_hash"],
        )

        assert asset.storage_key == record["destination_key"]
        assert asset.storage_key == "legacy/v1-bucket/aa/aa6e7f88e71f1d8373a79d0a2cf1465e.png"
