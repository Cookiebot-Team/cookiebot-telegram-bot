"""CLI entry point for the v1 import: `python scripts/cb.py import-mongo [...]`.

Deliberately thin — argument parsing, pool lifecycle and printing. Everything
that decides anything lives in `source.py`, `mappers.py` and `runner.py`, where
it can be tested without a process boundary.

    python scripts/cb.py import-mongo --dry-run
    python scripts/cb.py import-mongo --collections configs,rules
    CB_MONGO_DUMP_DIR=./dump python scripts/cb.py import-mongo
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from cb_core import db
from cb_core.logging import configure_logging, get_logger
from cb_core.settings import get_settings
from cb_worker.importer.runner import run_import
from cb_worker.importer.source import open_source

log = get_logger("cb.importer")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="cb.py import-mongo", description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="read and map everything, report the counts, write nothing",
    )
    parser.add_argument(
        "--collections",
        default="",
        help="comma-separated subset to import; default is every collection the source has",
    )
    parser.add_argument("--batch-size", type=int, default=0, help="override CB_IMPORT_BATCH_SIZE")
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    settings.service_name = "cb-importer"
    configure_logging(settings)

    collections = [c.strip() for c in args.collections.split(",") if c.strip()] or None
    batch_size = args.batch_size or settings.import_batch_size

    source = open_source(settings)
    try:
        # The schema has to exist before rows can land in it. Services converge it
        # at startup (cb_core/migrations.py); a one-shot CLI does not, so this
        # fails fast with a comprehensible error rather than a missing-table one.
        await db.init_pool(
            settings.model_copy(update={"pg_command_timeout": settings.import_command_timeout})
        )
        report = await run_import(
            source,
            collections=collections,
            dry_run=args.dry_run,
            batch_size=batch_size,
        )
    finally:
        source.close()
        await db.close_pool()

    print("\n".join(report.as_lines()))
    if args.dry_run:
        print("\ndry run: nothing was written")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except ValueError as exc:
        # open_source raises this for "no source configured" and "both configured",
        # which are user errors, not bugs — a traceback would bury the message.
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
