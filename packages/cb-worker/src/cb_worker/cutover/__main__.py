"""CLI entry point for the full cutover run: `python -m cb_worker.cutover [...]`,
wired to `python scripts/cb.py cutover`.

    python scripts/cb.py cutover --dry-run
    python scripts/cb.py cutover --only preflight,verify
    python scripts/cb.py cutover --skip memes --yes
    python scripts/cb.py cutover --only mongo --collections configs,rules --yes

Deliberately thin — argument parsing, step-name validation, the confirmation
prompt and the exit code live here; every decision that matters (what each
step does, how one step's failure never costs the rest) lives in `runner.py`,
where it is unit-tested without a process boundary. Same split
`cb_worker.importer.__main__` and `cb_worker.bucket_export.__main__` already use.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from rich.console import Console
from rich.prompt import Confirm

from cb_core.logging import configure_logging, get_logger
from cb_core.settings import Settings, get_settings
from cb_worker.cutover import STEP_ORDER, StepName, StepSelectionError, resolve_steps
from cb_worker.cutover.runner import render_summary, run_cutover
from cb_worker.meme_seed import DEFAULT_SOURCE

log = get_logger("cb.cutover")

_DEFAULT_BUCKET_MANIFEST = "bucket_export_manifest.jsonl"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="cb.py cutover",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--only",
        default="",
        help=f"comma-separated subset of steps to run; default is all of: {', '.join(STEP_ORDER)}",
    )
    parser.add_argument("--skip", default="", help="comma-separated steps to leave out of the run")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="every step reports what it would do and writes nothing",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="skip the confirmation prompt before a real (non-dry-run) run",
    )
    parser.add_argument(
        "--collections",
        default="",
        help="comma-separated subset passed to the mongo step; default is every collection the source has",
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=DEFAULT_SOURCE,
        help="v1 checkout root, passed to the memes step and checked by preflight (default: %(default)s)",
    )
    parser.add_argument(
        "--manifest",
        default=os.environ.get("CB_BUCKET_EXPORT_MANIFEST", _DEFAULT_BUCKET_MANIFEST),
        help="bucket export manifest path, passed to the bucket and verify steps (default: %(default)s)",
    )
    return parser.parse_args(argv)


def _missing_source(step: StepName, settings: Settings) -> str | None:
    """A step named explicitly in `--only` with nothing configured to run it
    against is a mistake worth catching before any work starts — in a full
    run the same gap is just a `"skipped"` row in the summary table (see
    `runner._step_mongo`/`_step_bucket`), because there the operator did not
    single that step out."""
    if step == "mongo" and not settings.mongo_uri.strip() and not settings.mongo_dump_dir.strip():
        return "cutover --only mongo needs CB_MONGO_URI or CB_MONGO_DUMP_DIR set"
    if step == "bucket" and not (
        os.environ.get("CB_BUCKET_EXPORT_DEST_URI", "").strip()
        and os.environ.get("CB_BUCKET_EXPORT_SOURCE_BUCKET", "").strip()
    ):
        return "cutover --only bucket needs CB_BUCKET_EXPORT_DEST_URI and CB_BUCKET_EXPORT_SOURCE_BUCKET set"
    return None


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    settings.service_name = "cb-cutover"
    configure_logging(settings)

    try:
        steps = resolve_steps(args.only, args.skip)
    except StepSelectionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if not steps:
        print("no steps selected — nothing to do")
        return 0

    if args.only:
        problems = [msg for step in steps if (msg := _missing_source(step, settings)) is not None]
        if problems:
            for msg in problems:
                print(f"error: {msg}", file=sys.stderr)
            return 2

    console = Console()

    if not args.dry_run and not args.yes and not settings.is_local:
        # A real run against anything but a local dev environment gets a
        # confirmation — unless stdin is not a TTY, in which case there is no
        # one to answer it and blocking would just hang a cutover script; ask
        # for `--yes` instead of a prompt no one can see.
        if not sys.stdin.isatty():
            print(
                "error: this is a real run against a non-local environment "
                f"(CB_ENV={settings.env!r}); pass --yes to confirm non-interactively, "
                "or run this from a terminal.",
                file=sys.stderr,
            )
            return 2
        console.print(f"about to run: {', '.join(steps)}  (CB_ENV={settings.env!r})")
        if not Confirm.ask(
            "proceed with a real (non-dry-run) cutover?", console=console, default=False
        ):
            print("aborted")
            return 2

    collections = [c.strip() for c in args.collections.split(",") if c.strip()] or None

    report = await run_cutover(
        settings,
        steps,
        dry_run=args.dry_run,
        collections=collections,
        memes_source=args.source,
        bucket_manifest_path=Path(args.manifest),
        console=console,
    )

    console.print(render_summary(report))
    if args.dry_run:
        console.print("\n[dim]dry run: nothing was written[/dim]")
    return 1 if report.any_failed() else 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
