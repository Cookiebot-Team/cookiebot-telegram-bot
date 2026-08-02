"""Drives one export run (or one `--verify` pass) and renders it with `rich`.

Idempotency and resumability both key off content: a blob is skipped, never
re-copied, once its content hash already has an object at the destination
(`keys.destination_key` is content-addressed, so that is one `store().exists()`
call), and a resumed run additionally trusts a prior manifest line for the same
`source_path` when the source's reported size still matches and the recorded
destination object still exists — which lets a resumed run skip the download
itself, not just the write. Either way, nothing is ever re-uploaded and nothing
is ever duplicated: see `_process_blob`.

Per-blob failures (an unreadable source object, a transient GCS error) are
caught here and turned into a `"failed"` manifest entry — they never abort the
run. That is the same contract `cb_worker.importer.runner` keeps for a bad
Mongo collection, for the same reason: on cutover day, one bad object must not
cost the other 111 MB of a 112 MB run.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn
from rich.table import Table

from cb_core.dedupe import fingerprint
from cb_core.logging import get_logger
from cb_core.storage.base import BlobStore, ObjectNotFoundError, StorageError
from cb_worker.bucket_export import (
    PREFIXES,
    BucketSource,
    ExportReport,
    ManifestEntry,
    Outcome,
    SourceBlob,
    VerifyProblem,
    VerifyReport,
)
from cb_worker.bucket_export import manifest as manifest_io
from cb_worker.bucket_export.keys import destination_key
from cb_worker.bucket_export.source import GcsSourceError

log = get_logger("cb.bucket_export.runner")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


async def _process_blob(
    source: BucketSource,
    store: BlobStore,
    prefix: str,
    blob: SourceBlob,
    previous: Mapping[str, ManifestEntry],
    *,
    dry_run: bool,
) -> ManifestEntry:
    """One blob, start to finish: resume-skip, or download + hash + dedupe-write.

    Never raises for a source-read failure — that becomes a `"failed"` entry
    instead, per the module docstring.
    """
    prior = previous.get(blob.name)
    if (
        prior is not None
        and prior.outcome in ("copied", "skipped")
        and prior.destination_key is not None
        and prior.byte_size == blob.size
        and await store.exists(prior.destination_key)
    ):
        # A previous run already landed this exact blob — same size reported by
        # the source, and the destination object it recorded is still there.
        # Trust it and skip the download entirely; this is what makes a
        # resumed run cheap, not just correct (correctness alone would be
        # satisfied by the content-hash check below on its own).
        return ManifestEntry(
            prefix=prefix,
            source_path=blob.name,
            byte_size=blob.size,
            content_hash=prior.content_hash,
            destination_key=prior.destination_key,
            outcome="skipped",
            detail="already exported (manifest + destination match)",
            exported_at=_now_iso(),
        )

    try:
        data = source.download(blob.name)
    except GcsSourceError as exc:
        log.warning("bucket_export.blob.download_failed", source_path=blob.name, error=str(exc))
        return ManifestEntry(
            prefix=prefix,
            source_path=blob.name,
            byte_size=blob.size,
            content_hash=None,
            destination_key=None,
            outcome="failed",
            detail=str(exc),
            exported_at=_now_iso(),
        )

    content_hash = fingerprint(data)
    dest_key = destination_key(content_hash, blob.name)

    outcome: Outcome
    if await store.exists(dest_key):
        # Genuine content-hash dedupe: this exact content already landed under
        # this key, whether from an earlier run or from a different source path
        # that happens to hold identical bytes.
        outcome = "skipped"
        detail = "content hash already present at destination"
    elif dry_run:
        outcome = "copied"
        detail = "dry run: would copy"
    else:
        await store.put(dest_key, data)
        outcome = "copied"
        detail = "copied"

    return ManifestEntry(
        prefix=prefix,
        source_path=blob.name,
        byte_size=len(data),
        content_hash=content_hash,
        destination_key=dest_key,
        outcome=outcome,
        detail=detail,
        exported_at=_now_iso(),
    )


async def run_export(
    source: BucketSource,
    store: BlobStore,
    *,
    prefixes: Sequence[str] = PREFIXES,
    manifest_path: Path,
    dry_run: bool = False,
    console: Console | None = None,
) -> ExportReport:
    """Export every prefix, in order, with a `rich` progress bar per prefix.

    `dry_run=True` still lists, downloads and hashes everything — the only
    things it skips are `store().put()` and the manifest append, so the
    printed summary is exactly the counts a real run would produce (module
    docstring, "Idempotency and resumability"). A real run additionally reads
    the existing manifest first so a resumed run can skip what a prior run
    already landed (see `_process_blob`).
    """
    console = console or Console()
    report = ExportReport()
    previous = manifest_io.latest_by_source(manifest_path)

    with Progress(
        TextColumn("[bold blue]{task.fields[prefix]}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as progress:
        for prefix in prefixes:
            stats = report.stats_for(prefix)
            try:
                blobs = list(source.list_prefix(prefix))
            except GcsSourceError as exc:
                # A whole prefix failing to list (bad prefix, transient GCS
                # error) is still not fatal to the run — record it and move on,
                # same "never abandon the other prefixes" contract as a
                # per-blob failure.
                log.warning("bucket_export.prefix.list_failed", prefix=prefix, error=str(exc))
                report.failures.append(
                    ManifestEntry(
                        prefix=prefix,
                        source_path="*",
                        byte_size=0,
                        content_hash=None,
                        destination_key=None,
                        outcome="failed",
                        detail=f"prefix listing failed: {exc}",
                        exported_at=_now_iso(),
                    )
                )
                continue

            task_id = progress.add_task("", prefix=prefix, total=len(blobs))
            for blob in blobs:
                entry = await _process_blob(source, store, prefix, blob, previous, dry_run=dry_run)
                stats.found += 1
                if entry.outcome == "copied":
                    stats.copied += 1
                    stats.bytes_copied += entry.byte_size
                elif entry.outcome == "skipped":
                    stats.skipped += 1
                else:
                    stats.failed += 1
                    report.failures.append(entry)

                if not dry_run:
                    manifest_io.append(manifest_path, entry)
                progress.update(task_id, advance=1)

    log.info(
        "bucket_export.run.done",
        dry_run=dry_run,
        found=report.total_found(),
        copied=report.total_copied(),
        skipped=report.total_skipped(),
        failed=report.total_failed(),
    )
    return report


async def verify_manifest(store: BlobStore, manifest_path: Path) -> VerifyReport:
    """Re-read the manifest and confirm every destination object this run
    claims to have landed still exists, with the expected size and content
    hash — the input this tool promises for the verification pass, and what a
    human trusts before declaring cutover done.
    """
    if not manifest_io.path_exists(manifest_path):
        raise ValueError(
            f"no manifest at {manifest_path} — run an export (without --verify) first, "
            "the manifest is what --verify reads"
        )

    report = VerifyReport()
    for entry in manifest_io.latest_by_source(manifest_path).values():
        if entry.outcome not in ("copied", "skipped"):
            continue  # a "failed" entry never had a destination to check
        if entry.destination_key is None or entry.content_hash is None:
            continue

        report.checked += 1
        try:
            data = await store.get(entry.destination_key)
        except ObjectNotFoundError:
            report.problems.append(
                VerifyProblem(
                    entry.source_path,
                    entry.destination_key,
                    "missing",
                    "destination object not found",
                )
            )
            continue
        except StorageError as exc:
            report.problems.append(
                VerifyProblem(entry.source_path, entry.destination_key, "error", str(exc))
            )
            continue

        if len(data) != entry.byte_size:
            report.problems.append(
                VerifyProblem(
                    entry.source_path,
                    entry.destination_key,
                    "size_mismatch",
                    f"expected {entry.byte_size} bytes, found {len(data)}",
                )
            )
            continue

        actual_hash = fingerprint(data)
        if actual_hash != entry.content_hash:
            report.problems.append(
                VerifyProblem(
                    entry.source_path,
                    entry.destination_key,
                    "hash_mismatch",
                    f"expected {entry.content_hash}, computed {actual_hash}",
                )
            )
            continue

        report.ok += 1

    log.info(
        "bucket_export.verify.done",
        checked=report.checked,
        ok=report.ok,
        problems=len(report.problems),
    )
    return report


def _human_bytes(n: int) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}TB"


def render_summary(report: ExportReport) -> Table:
    """prefix, found, copied, skipped, failed, bytes — one row per prefix plus
    a totals row, in the same order the run processed prefixes."""
    table = Table(title="bucket export summary")
    table.add_column("prefix")
    table.add_column("found", justify="right")
    table.add_column("copied", justify="right")
    table.add_column("skipped", justify="right")
    table.add_column("failed", justify="right")
    table.add_column("bytes", justify="right")
    for prefix, stats in report.prefixes.items():
        table.add_row(
            prefix,
            str(stats.found),
            str(stats.copied),
            str(stats.skipped),
            str(stats.failed),
            _human_bytes(stats.bytes_copied),
        )
    table.add_section()
    table.add_row(
        "TOTAL",
        str(report.total_found()),
        str(report.total_copied()),
        str(report.total_skipped()),
        str(report.total_failed()),
        _human_bytes(report.total_bytes()),
        style="bold",
    )
    return table


def render_verify(report: VerifyReport) -> Table:
    table = Table(title="bucket export verify")
    table.add_column("source path")
    table.add_column("status")
    table.add_column("detail")
    for problem in report.problems:
        table.add_row(problem.source_path, problem.status, problem.detail)
    if not report.problems:
        table.add_row(f"all {report.ok} verified objects match", "ok", "")
    return table


__all__ = ["render_summary", "render_verify", "run_export", "verify_manifest"]
