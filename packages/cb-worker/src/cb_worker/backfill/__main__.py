"""CLI entry point for the random-media backfill:
`python scripts/cb.py backfill-random [...]`.

Deliberately thin — argument parsing, the bot and pool lifecycle, and
printing. Everything that decides anything lives in `random_media.py`, where it
is unit-tested without a process boundary, the same split
`cb_worker.importer.__main__` already makes.

    python scripts/cb.py backfill-random --dry-run
    python scripts/cb.py backfill-random --limit 50
    CB_MONGO_DUMP_DIR=./dump python scripts/cb.py backfill-random --skin cookiebot

Run `import-mongo` first: `media_objects.group_id` is a foreign key to
`groups`, so a pointer for a group that has not been imported yet is skipped
with that reason rather than failing.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties

from cb_core import db, storage
from cb_core.logging import configure_logging, get_logger
from cb_core.settings import get_settings
from cb_worker.backfill import BackfillReport
from cb_worker.backfill.random_media import run_backfill
from cb_worker.importer.source import open_source

log = get_logger("cb.backfill")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="cb.py backfill-random", description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="read every pointer and report what would be downloaded, write nothing",
    )
    parser.add_argument(
        "--skin",
        default="",
        help=(
            "which bot token to resolve file ids with (default: the first in "
            "CB_BOT_TOKENS). A file_id only resolves for the bot that saw the message."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="stop after this many pointers; 0 means every one",
    )
    return parser.parse_args(argv)


def _render(report: BackfillReport) -> str:
    lines = [
        f"read      {report.read}",
        f"imported  {report.imported}",
        f"skipped   {report.skipped}",
        f"failed    {report.failed}",
    ]
    if report.results:
        lines.append("")
        lines.append("not imported:")
        # Grouped by reason rather than listed row by row: a run over a real
        # dump produces thousands of "already imported" lines otherwise, and
        # the interesting number is how many of each kind there are.
        by_detail: dict[str, int] = {}
        for result in report.results:
            by_detail[result.detail.split(":")[0]] = (
                by_detail.get(result.detail.split(":")[0], 0) + 1
            )
        lines.extend(f"  {count:>7}  {detail}" for detail, count in sorted(by_detail.items()))
    return "\n".join(lines)


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    settings.service_name = "cb-backfill"
    configure_logging(settings)

    tokens = settings.bot_tokens
    if not tokens:
        print("error: CB_BOT_TOKENS is empty; a file_id needs a bot to resolve it", file=sys.stderr)
        return 2
    skin = args.skin or next(iter(tokens))
    token = tokens.get(skin)
    if token is None:
        print(f"error: no token for skin {skin!r} in CB_BOT_TOKENS", file=sys.stderr)
        return 2

    source = open_source(settings)
    bot = Bot(token=token, default=DefaultBotProperties(parse_mode="HTML"))
    try:
        await db.init_pool(
            settings.model_copy(update={"pg_command_timeout": settings.import_command_timeout})
        )
        await storage.init_storage(settings)
        report = await run_backfill(
            source,
            bot,
            dry_run=args.dry_run,
            limit=args.limit or None,
        )
    finally:
        source.close()
        await bot.session.close()
        await storage.close_storage()
        await db.close_pool()

    print(_render(report))
    if args.dry_run:
        print("\ndry run: nothing was downloaded or written")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except ValueError as exc:
        # `open_source` raises this for "no source configured" and "both
        # configured" — user errors, not bugs.
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
