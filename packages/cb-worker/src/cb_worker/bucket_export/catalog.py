"""`legacy-catalog`: turn a finished bucket-export manifest into the small
per-prefix CSV catalogs `cb_core.legacy_assets` reads.

    python scripts/cb.py legacy-catalog [--manifest PATH] [--output DIR] [--dry-run]

`cb_worker.bucket_export` already put every blob's bytes in `cb_core.storage`
under a content-addressed key; what it does not leave anywhere convenient is
*which v1 prefix* a given blob came from — that mapping only survives in the
manifest (`manifest.py`), one JSON line per (source blob, outcome). This
module reads that manifest and regroups it by prefix into the catalog shape
`cb_core.legacy_assets` ships as package data — the same "tiny catalog as
package data, bytes in `cb_core.storage`" split `meme_templates.py`'s own
docstring documents for `meme_metadata.csv`.

Belongs under `bucket_export/`, not in `cb_core`, for the same reason the rest
of this package does: it is cutover-day tooling, run once by a human against
a finished export (`cb_core.legacy_assets`'s own docstring), not code any
request path imports. The one piece that *does* live in `cb_core` is the
prefix -> filename naming (`cb_core.legacy_assets.catalog_relpath`), imported
from there rather than redefined here — see that function's docstring for why
the writer and the reader must never be free to disagree about it.

**Only `"copied"` and `"skipped"` rows are ever emitted.** A `"failed"` row
(`ManifestEntry.outcome`) describes a blob that was never written to the
destination (`bucket_export/__init__.py`'s own docstring, `ManifestEntry`);
putting it in a catalog would hand a future feature a key `store().get()`
raises `ObjectNotFoundError` for. `manifest.latest_by_source` is read first
for the same reason `runner.py` reads it before resuming a run: a resumed
export can turn an earlier `"failed"` row into a later `"copied"` one for the
same `source_path`, and only the latest verdict should ever reach a catalog.
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import sys
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console
from rich.table import Table

from cb_core.legacy_assets import catalog_relpath
from cb_core.logging import get_logger
from cb_worker.bucket_export import PREFIXES, ManifestEntry
from cb_worker.bucket_export import manifest as manifest_io

log = get_logger("cb.bucket_export.catalog")

#: Fixed here rather than derived from `ManifestEntry`'s own field list, so a
#: future manifest field (say, a content type) does not silently widen every
#: catalog — "what a consumer needs and nothing more" is a deliberate subset
#: of the manifest, not "whatever the manifest happens to carry".
CSV_FIELDS: tuple[str, str, str, str] = (
    "source_path",
    "destination_key",
    "byte_size",
    "content_hash",
)

#: The literal `ManifestEntry.prefix` value `PREFIXES` uses for the one
#: dynamic entry — matched exactly, including the trailing slash
#: (`bucket_export/__init__.py`'s own `PREFIXES` docstring on why the slash
#: is never normalised away).
_CUSTOM_PREFIX = "Custom/"

#: Where a fresh checkout's catalogs land: computed from this file's own
#: location rather than hardcoded to an absolute path, so the default works
#: the same whether `cb.py` is invoked from the repo root or a CI checkout
#: rooted somewhere else.
DEFAULT_OUTPUT_ROOT = (
    Path(__file__).resolve().parents[4] / "cb-core" / "src" / "cb_core" / "asset_data" / "legacy"
)

_DEFAULT_MANIFEST = "bucket_export_manifest.jsonl"


def is_folder_placeholder(source_path: str) -> bool:
    """A GCS "folder" object rather than a file — a zero-byte object whose
    name ends in `/`, which the Cloud Console creates when someone makes a
    folder by hand and which `list_blobs` returns like any other blob.

    v1 never had to care: `random.choice(bloblist_x)` could draw one, and
    `generate_signed_url` on a zero-byte object still produced a URL
    Telegram would then refuse — a rare, invisible failure in a fun command.
    Here it would be a pool entry `legacy_assets.choose` hands a handler,
    which then sends an empty `BufferedInputFile`. Five of them came out of
    the real export (`Countdown/Furcamp/`, `Countdown/Pawstral/`,
    `Custom/{akiiny,dragoonie,meleys}/`) — and they are the same two "extra"
    objects the bucket's own listing count (6,912) has over the file count
    (6,910), plus three inside `Custom/`.

    Dropped at catalog time rather than at read time: the export itself
    should keep copying them (the manifest is an audit log of what the
    bucket held, not of what a feature can use), and every consumer would
    otherwise have to re-derive the same rule.
    """
    return source_path.endswith("/")


@dataclass(frozen=True, slots=True)
class CatalogRow:
    """One row of a generated catalog — the exact fields
    `cb_core.legacy_assets.LegacyAsset` reads back."""

    source_path: str
    destination_key: str
    byte_size: int
    content_hash: str


@dataclass(slots=True)
class CatalogStats:
    """One row of `render_summary`'s table: one prefix, its on-disk relpath,
    and how many rows it carries."""

    key: str
    relpath: str
    rows: int


@dataclass(slots=True)
class CatalogReport:
    """What one `write_catalogs` (or `--dry-run`) call would write, or wrote.

    `unknown_prefixes` is every literal `ManifestEntry.prefix` seen that is
    neither in `known_prefixes` nor the dynamic `"Custom/"` case — reported
    here (task description's "reported, not silently dropped") rather than
    excluded from `catalogs`: an unknown prefix's rows still land in their
    own catalog file exactly like a known one, because the alternative —
    dropping them — would silently throw away real exported bytes over what
    might be nothing worse than `PREFIXES` not having been told about a new
    v1 folder yet.
    """

    catalogs: dict[str, CatalogStats] = field(default_factory=dict)
    unknown_prefixes: tuple[str, ...] = ()
    #: How many manifest rows `is_folder_placeholder` dropped. Counted and
    #: printed rather than silently skipped, for the same reason
    #: `unknown_prefixes` is: a number that changes between two exports of
    #: the same bucket is worth someone noticing.
    placeholders: int = 0

    def total_rows(self) -> int:
        return sum(stats.rows for stats in self.catalogs.values())


def _catalog_key(entry: ManifestEntry) -> str:
    """`ManifestEntry.prefix` -> the grouping key `catalog_relpath` turns
    into a filename. Every prefix is already that key verbatim except the
    dynamic `Custom/` one, whose real grouping key is one level more
    specific than the manifest's own `prefix` field: v1 lists `Custom/` as a
    whole (`Miscellaneous.py:23`) but dispatches per sub-folder
    (`:147`, `custom_command`), so a catalog keyed on bare `"Custom/"` would
    mix every custom command's images into one undifferentiated pool — the
    opposite of what `/customcommand` needs. The command name is the first
    path segment of `source_path` after the prefix (`"Custom/<command>/<file>"`,
    the same shape v1's own listing walks).
    """
    if entry.prefix != _CUSTOM_PREFIX:
        return entry.prefix
    segments = entry.source_path.split("/", 2)
    if len(segments) < 2 or not segments[1]:
        raise ValueError(
            f"Custom/ manifest entry with no command sub-folder: {entry.source_path!r}"
        )
    return f"{_CUSTOM_PREFIX}{segments[1]}"


def build_catalogs(
    manifest_path: Path,
    *,
    known_prefixes: Sequence[str] = PREFIXES,
) -> tuple[dict[str, list[CatalogRow]], tuple[str, ...], int]:
    """Group `manifest_path`'s latest-per-source entries into per-catalog row
    lists, keyed the same way `catalog_relpath` expects. Returns
    `(catalogs, unknown_prefixes, placeholders)` — see `CatalogReport` for
    what each means.

    Deterministic: each catalog's rows are sorted by `source_path` before
    this function returns, so two builds from the same manifest produce
    byte-identical CSVs regardless of manifest line order or dict iteration
    order (task description's "regenerating from the same manifest produces
    a byte-identical file").
    """
    known = set(known_prefixes)
    groups: dict[str, list[CatalogRow]] = defaultdict(list)
    unknown: set[str] = set()
    placeholders = 0

    for entry in manifest_io.latest_by_source(manifest_path).values():
        if entry.outcome not in ("copied", "skipped"):
            continue  # a "failed" row has no object at the destination (module docstring)
        if entry.destination_key is None or entry.content_hash is None:
            # Never true for "copied"/"skipped" per `ManifestEntry`'s own
            # contract, but a manifest is an external file on disk — trust
            # nothing at that seam, even a shape mypy already believes.
            continue
        if is_folder_placeholder(entry.source_path):
            # Not a file — a folder marker no feature can send (see
            # `is_folder_placeholder`). Counted, not silently dropped.
            placeholders += 1
            continue

        if entry.prefix != _CUSTOM_PREFIX and entry.prefix not in known:
            unknown.add(entry.prefix)

        groups[_catalog_key(entry)].append(
            CatalogRow(
                source_path=entry.source_path,
                destination_key=entry.destination_key,
                byte_size=entry.byte_size,
                content_hash=entry.content_hash,
            )
        )

    for rows in groups.values():
        rows.sort(key=lambda row: row.source_path)

    return dict(groups), tuple(sorted(unknown)), placeholders


def render_csv(rows: Sequence[CatalogRow]) -> str:
    """One catalog file's full text, header included. `lineterminator="\\n"`
    pins the output to `\\n` regardless of platform, which is what makes two
    builds byte-identical rather than merely row-identical.
    """
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(CSV_FIELDS)
    for row in rows:
        writer.writerow([row.source_path, row.destination_key, row.byte_size, row.content_hash])
    return buf.getvalue()


def write_catalogs(
    manifest_path: Path,
    output_root: Path,
    *,
    known_prefixes: Sequence[str] = PREFIXES,
    dry_run: bool = False,
) -> CatalogReport:
    """Build every catalog and, unless `dry_run`, write each to
    `output_root / catalog_relpath(key)`.

    `dry_run=True` still does the full grouping pass — the report it returns
    carries the same counts a real run would — the only thing it skips is the
    `Path.write_text` call, same "predict exactly, write nothing" contract
    `bucket_export.runner.run_export`'s own `dry_run` keeps.
    """
    groups, unknown, placeholders = build_catalogs(manifest_path, known_prefixes=known_prefixes)
    report = CatalogReport(unknown_prefixes=unknown, placeholders=placeholders)

    for key in sorted(groups):
        rows = groups[key]
        relpath = catalog_relpath(key)
        report.catalogs[key] = CatalogStats(key=key, relpath=relpath, rows=len(rows))
        if dry_run:
            continue
        destination = output_root / relpath
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(render_csv(rows), encoding="utf-8")

    log.info(
        "legacy_catalog.build.done",
        dry_run=dry_run,
        catalogs=len(report.catalogs),
        rows=report.total_rows(),
        unknown_prefixes=len(report.unknown_prefixes),
        placeholders=report.placeholders,
    )
    return report


def render_summary(report: CatalogReport) -> Table:
    """One row per catalog plus a totals row — the `rich` table
    `catalog_cli.main` prints, same house style `bucket_export.runner
    .render_summary` uses for its own per-prefix table."""
    table = Table(title="legacy catalog summary")
    table.add_column("prefix")
    table.add_column("catalog file")
    table.add_column("rows", justify="right")
    for key in sorted(report.catalogs):
        stats = report.catalogs[key]
        table.add_row(stats.key, stats.relpath, str(stats.rows))
    table.add_section()
    table.add_row("TOTAL", "", str(report.total_rows()), style="bold")
    if report.placeholders:
        table.add_row(
            "folder placeholders", "dropped (not files)", str(report.placeholders), style="dim"
        )
    return table


def render_unknown(report: CatalogReport) -> Table | None:
    """A second table flagging any manifest prefix `PREFIXES` does not know
    about, or `None` when there are none — printed separately from
    `render_summary` so an unknown prefix cannot be mistaken for routine
    output (task description's "reported, not silently dropped")."""
    if not report.unknown_prefixes:
        return None
    table = Table(title="unknown prefixes (not in PREFIXES)")
    table.add_column("prefix")
    for prefix in report.unknown_prefixes:
        table.add_row(prefix)
    return table


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="cb.py legacy-catalog", description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path(os.environ.get("CB_BUCKET_EXPORT_MANIFEST", _DEFAULT_MANIFEST)),
        help="path to the bucket-export manifest (default: %(default)s)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help="where to write the catalogs (default: cb_core's asset_data/legacy)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would be written, write nothing",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    console = Console()

    if not manifest_io.path_exists(args.manifest):
        print(f"error: no manifest at {args.manifest}", file=sys.stderr)
        return 2

    report = write_catalogs(args.manifest, args.output, dry_run=args.dry_run)

    console.print(render_summary(report))
    unknown_table = render_unknown(report)
    if unknown_table is not None:
        console.print(unknown_table)
    if args.dry_run:
        console.print("\n[dim]dry run: nothing was written[/dim]")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    sys.exit(main())


__all__ = [
    "CSV_FIELDS",
    "DEFAULT_OUTPUT_ROOT",
    "CatalogReport",
    "CatalogRow",
    "CatalogStats",
    "build_catalogs",
    "is_folder_placeholder",
    "main",
    "render_csv",
    "render_summary",
    "render_unknown",
    "write_catalogs",
]
