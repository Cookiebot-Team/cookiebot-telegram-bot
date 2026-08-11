"""Move v1's meme templates into v2 object storage. Cutover tooling, run once.

    python scripts/cb.py meme-seed [--source DIR] [--dry-run] [--verify] [--force]

`cb_core.meme_templates` ships v1's `meme_metadata.csv` as package data (97 kB)
and describes where each template's bytes belong; this puts the bytes there.
110 MB across 801 files is too much for a wheel, and unlike `media_objects`
these are bot-owned and global — no `group_id` to distribute them by — so they
go through `cb_core.storage.store()`, exactly the reasoning
`.specs/features/platform_bucket_export/spec.md` gives for the GCS export.

Deliberately *not* part of `bucket_export`: that tool reads v1's private GCS
bucket with a read-only-scoped credential nobody in this environment has, while
these templates are checked into the v1 repo and are a plain directory copy.
One tool per source, both idempotent, neither blocked on the other.

Idempotent by key, not by content hash: a template's identity is the filename
v1 gave it, because the CSV refers to it by that name. A re-run skips keys that
are already present at the same size; `--force` overwrites them.
"""

from __future__ import annotations

import argparse
import asyncio
import mimetypes
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from cb_core import storage
from cb_core.logging import get_logger
from cb_core.meme_templates import MemeTemplate, all_templates
from cb_core.settings import get_settings

log = get_logger("cb.worker.meme_seed")

#: Where the templates sit in a v1 checkout, relative to the source root.
V1_SUBPATH = "Bot/Static/Meme"

#: The default source, the same "one directory up" convention AGENTS.md §1 uses
#: for every other reference to the v1 repository.
DEFAULT_SOURCE = Path("../COOKIEBOT-Telegram-Group-Bot")


@dataclass
class SeedReport:
    copied: int = 0
    skipped: int = 0
    missing: list[str] | None = None
    failed: list[str] | None = None

    def __post_init__(self) -> None:
        self.missing = [] if self.missing is None else self.missing
        self.failed = [] if self.failed is None else self.failed

    @property
    def ok(self) -> bool:
        return not self.missing and not self.failed


def source_path(root: Path, template: MemeTemplate) -> Path:
    """Where this template's bytes are in a v1 checkout.

    Built from the language and filename rather than the CSV's `full_path`
    column: that column is relative to v1's *working directory*
    (`Static/Meme/...`), which only resolves if you happen to be standing in
    `Bot/`.
    """
    return root / V1_SUBPATH / template.language / template.filename


async def seed(
    root: Path,
    *,
    dry_run: bool = False,
    force: bool = False,
    verify: bool = False,
    on_template: Callable[[MemeTemplate, str], None] | None = None,
) -> SeedReport:
    """`on_template(template, outcome)` fires exactly once per catalog entry,
    after its fate for this run is decided (`"copied"`, `"skipped"`, `"missing"`,
    `"present"` — verify's affirmative case — or `"failed"`). It exists so a
    caller that wants a progress bar (`cb_worker.cutover`) can drive one without
    this module importing `rich` itself — the same "pure logic, no rich" split
    `cb_worker.importer.runner` keeps for its own optional collection callbacks.
    Default `None` leaves every existing caller and test byte-for-byte unchanged.
    """
    report = SeedReport()
    store = storage.store()

    for template in all_templates():
        key = template.storage_key
        path = source_path(root, template)
        outcome: str

        if verify:
            if not await store.exists(key):
                report.missing.append(key)  # type: ignore[union-attr]
                outcome = "missing"
            else:
                report.skipped += 1
                outcome = "present"
        elif not path.is_file():
            report.missing.append(str(path))  # type: ignore[union-attr]
            outcome = "missing"
        elif not force and await store.exists(key):
            report.skipped += 1
            outcome = "skipped"
        elif dry_run:
            report.copied += 1
            outcome = "copied"
        else:
            try:
                content_type, _ = mimetypes.guess_type(template.filename)
                await store.put(key, path.read_bytes(), content_type=content_type or "image/jpeg")
            except Exception as exc:  # noqa: BLE001 - one bad file must not end the run
                log.warning("meme_seed.put_failed", key=key, error=str(exc))
                report.failed.append(key)  # type: ignore[union-attr]
                outcome = "failed"
            else:
                report.copied += 1
                outcome = "copied"

        if on_template is not None:
            on_template(template, outcome)

    return report


async def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cb.py meme-seed", description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help="v1 checkout root")
    parser.add_argument("--dry-run", action="store_true", help="count, copy nothing")
    parser.add_argument("--force", action="store_true", help="overwrite keys already present")
    parser.add_argument(
        "--verify", action="store_true", help="read nothing from v1; report missing keys"
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    if settings.storage_uri.startswith("memory://"):
        # A memory store is per-process: seeding it would write 110 MB into a
        # process that exits immediately afterwards, which reads as success and
        # leaves the feature dead. Refuse rather than pretend.
        print(
            "error: CB_STORAGE_URI is memory://, which does not survive this process."
            " Point it at s3://, gs:// or file:// before seeding.",
            file=sys.stderr,
        )
        return 2

    await storage.init_storage(settings)
    try:
        report = await seed(args.source, dry_run=args.dry_run, force=args.force, verify=args.verify)
    finally:
        await storage.close_storage()

    print(f"copied {report.copied}, skipped {report.skipped}")
    for path in report.missing or ():
        print(f"missing: {path}", file=sys.stderr)
    for key in report.failed or ():
        print(f"failed:  {key}", file=sys.stderr)
    return 0 if report.ok else 1


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_main(argv))


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())


__all__ = ["DEFAULT_SOURCE", "V1_SUBPATH", "SeedReport", "main", "seed", "source_path"]
