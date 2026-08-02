"""The manifest: the audit trail for cutover day and the resume/verify input.

One JSON object per line, one line appended per source blob a run touches —
never rewritten, never truncated, so a process killed mid-run loses nothing it
already recorded. That append-only shape is also what makes the file trivially
diffable and greppable by a human on the day, which matters as much as the
machine-readable side: "did `Fight/Portuguese/ronin.png` copy?" should be a
`grep`, not a script.

A source path can appear more than once across runs (a resumed run touches
everything again). `latest_by_source` keeps only the last line per path —
later runs win, same "last write wins" semantics `TableLoad`-style upserts use
elsewhere in this codebase — which is what `runner.py` reads back to decide
what a fresh run can skip re-downloading, and what `--verify` checks against
the destination.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

from cb_worker.bucket_export import ManifestEntry


def path_exists(path: Path) -> bool:
    """Thin wrapper so an async caller never calls `pathlib.Path` methods
    directly (ruff ASYNC240) for what is a one-time, few-KB stat, not a hot
    path worth an anyio/trio path type."""
    return path.exists()


def append(path: Path, entry: ManifestEntry) -> None:
    """Append one line and flush — the durability point a resumed run relies on."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(
        {
            "prefix": entry.prefix,
            "source_path": entry.source_path,
            "byte_size": entry.byte_size,
            "content_hash": entry.content_hash,
            "destination_key": entry.destination_key,
            "outcome": entry.outcome,
            "detail": entry.detail,
            "exported_at": entry.exported_at,
        },
        ensure_ascii=False,
    )
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line)
        fh.write("\n")
        fh.flush()


def read_all(path: Path) -> Iterator[ManifestEntry]:
    """Every line, in file order. Silently absent file yields nothing — a first
    run has no manifest yet, which is not an error."""
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                data = json.loads(raw)
                yield ManifestEntry(
                    prefix=data["prefix"],
                    source_path=data["source_path"],
                    byte_size=data["byte_size"],
                    content_hash=data["content_hash"],
                    destination_key=data["destination_key"],
                    outcome=data["outcome"],
                    detail=data["detail"],
                    exported_at=data["exported_at"],
                )
            except (json.JSONDecodeError, KeyError) as exc:
                raise ValueError(f"{path}:{lineno}: malformed manifest line: {exc}") from exc


def latest_by_source(path: Path) -> dict[str, ManifestEntry]:
    """The most recent entry per `source_path` — what a resumed run trusts."""
    latest: dict[str, ManifestEntry] = {}
    for entry in read_all(path):
        latest[entry.source_path] = entry
    return latest


__all__ = ["append", "latest_by_source", "read_all"]
