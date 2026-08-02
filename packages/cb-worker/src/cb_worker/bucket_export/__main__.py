"""CLI entry point for the cutover bucket export: `python -m cb_worker.bucket_export [...]`,
wired to `python scripts/cb.py bucket-export`.

    CB_BUCKET_EXPORT_SOURCE_BUCKET=cookiebot-bucket \\
    CB_BUCKET_EXPORT_DEST_URI=s3://cookiebot-legacy-assets \\
    CB_BUCKET_EXPORT_DEST_ENDPOINT=https://<account-id>.r2.cloudflarestorage.com \\
    CB_BUCKET_EXPORT_DEST_REGION=auto \\
    python scripts/cb.py bucket-export --dry-run

Deliberately thin — argument parsing, env lookup and store lifecycle — the same
split `cb_worker.importer.__main__` uses. Every decision that matters lives in
`source.py`, `runner.py`, `manifest.py` and `keys.py`, where it is unit-tested
without a process boundary or real GCS/R2 credentials.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from rich.console import Console

from cb_core.logging import configure_logging, get_logger
from cb_core.settings import get_settings
from cb_core.storage import store_from_uri
from cb_worker.bucket_export import PREFIXES
from cb_worker.bucket_export.runner import (
    render_summary,
    render_verify,
    run_export,
    verify_manifest,
)
from cb_worker.bucket_export.source import GcsSourceError, open_source

log = get_logger("cb.bucket_export")

_DEFAULT_MANIFEST = "bucket_export_manifest.jsonl"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="cb.py bucket-export", description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="list, download and hash every blob, print what a real run would do, write nothing",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="re-read the manifest and confirm every destination object matches; touches no v1 source",
    )
    parser.add_argument(
        "--prefixes",
        default="",
        help="comma-separated subset of prefixes; default is every known v1 prefix",
    )
    parser.add_argument(
        "--manifest",
        default=os.environ.get("CB_BUCKET_EXPORT_MANIFEST", _DEFAULT_MANIFEST),
        help="path to the manifest file (default: %(default)s)",
    )
    return parser.parse_args(argv)


def _dest_store_options() -> dict[str, str]:
    """R2 is S3-compatible but needs a non-AWS endpoint; obstore's `S3Store`
    takes that (and region) as extra keyword options passed straight through
    `store_from_uri` (`obstore_backend.py`'s own docstring: "region, endpoint,
    anonymous access, ... so a MinIO or fake-GCS endpoint works in CI" — R2 is
    the same mechanism, a real cloud instead of a CI fake)."""
    options: dict[str, str] = {}
    endpoint = os.environ.get("CB_BUCKET_EXPORT_DEST_ENDPOINT", "").strip()
    region = os.environ.get("CB_BUCKET_EXPORT_DEST_REGION", "").strip()
    if endpoint:
        options["endpoint"] = endpoint
    if region:
        options["region"] = region
    return options


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    settings.service_name = "cb-bucket-export"
    configure_logging(settings)

    manifest_path = Path(args.manifest)

    dest_uri = os.environ.get("CB_BUCKET_EXPORT_DEST_URI", "").strip()
    if not dest_uri:
        raise ValueError(
            "CB_BUCKET_EXPORT_DEST_URI is not set — the export needs to know where in v2 "
            "object storage to land the copies (e.g. 's3://cookiebot-legacy-assets' for "
            "Cloudflare R2, with CB_BUCKET_EXPORT_DEST_ENDPOINT set to the account's R2 endpoint)"
        )
    store = store_from_uri(dest_uri, **_dest_store_options())

    console = Console()
    try:
        if args.verify:
            report = await verify_manifest(store, manifest_path)
            console.print(render_verify(report))
            return 0 if not report.problems else 1

        prefixes = tuple(p.strip() for p in args.prefixes.split(",") if p.strip()) or PREFIXES
        source_bucket = os.environ.get("CB_BUCKET_EXPORT_SOURCE_BUCKET", "").strip()
        source = open_source(source_bucket)
        try:
            export_report = await run_export(
                source,
                store,
                prefixes=prefixes,
                manifest_path=manifest_path,
                dry_run=args.dry_run,
                console=console,
            )
        finally:
            source.close()

        console.print(render_summary(export_report))
        if args.dry_run:
            console.print("\n[dim]dry run: nothing was written[/dim]")
        return 0 if export_report.total_failed() == 0 else 1
    finally:
        await store.close()


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except (ValueError, GcsSourceError) as exc:
        # A missing CB_BUCKET_EXPORT_DEST_URI, a missing source bucket name, or
        # no usable Google credentials all raise one of these — user errors,
        # not bugs, so a one-line message beats a traceback here (the same
        # contract `cb_worker.importer.__main__` keeps for its own source
        # configuration errors).
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
