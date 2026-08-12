"""Cutover bucket export: v1's private GCS bucket -> v2 object storage.

Runs once, for real, on cutover day, and a second time straight after to catch
whatever changed in between — the same shape as `cb_worker.importer` for the
Mongo -> Citus move (see that package's docstring), for the same reason: there
is no maintenance window, so the tool has to be safe to run twice.

Three layers, deliberately separate, mirroring the importer's split:

* `source.py`   — where blobs come from: v1's GCS bucket, read through a client
                  scoped to `devstorage.read_only` and wrapped so the only
                  operations available are `list_prefix` and `download`. See
                  that module's docstring for why this is enforced three ways,
                  not just documented.
* `manifest.py` — the audit trail. One JSON line per (source blob, this run's
                  outcome), appended as the run goes so a crash mid-run loses
                  nothing already written. Read back by a resumed run (to skip
                  what a prior run already copied) and by `--verify`.
* `runner.py`   — drives one run: list each prefix, hash each blob, write it to
                  `cb_core.storage.store()` if its content hash is not already
                  there, record the outcome, render progress and the summary
                  table with `rich`.

## What gets copied

Derived from every `list_blobs(prefix=...)` call in the v1 checkout
(`../COOKIEBOT-Telegram-Group-Bot`), not from a prefix list handed down by
anyone — see `PREFIXES` below for the full inventory and where each one comes
from. `Custom/` is the one dynamic case: v1 discovers custom-command names by
listing that prefix rather than hardcoding them
(`Bot/Miscellaneous.py:23,147`), so this tool lists it the same way instead of
enumerating names.

**Deliberately excluded**: `cookiebot-bucket-public`'s `chatpfp/` prefix
(`Bot/Configurations.py:8,30`). That is a *different* bucket from the one this
tool reads, and v1 itself writes to it (`blob.upload_from_filename`, ibid.) —
it is a cache of Telegram-hosted chat photos, not source-of-truth static
content, so it fails the "the source bucket is read-only" premise this tool is
built around. v2's own group-photo caching, whenever it exists, repopulates it
straight from Telegram; there is nothing here worth migrating byte-for-byte.

## Where the copies land

`cb_core.storage.store()`, not `.media()`. `media()` is the per-group
reference-row layer (`media_objects.group_id` is `NOT NULL` and is the Citus
distribution column) — every blob this tool moves is bot-owned and global
(a `/death` gif, a `/battle` fighter portrait, a `/rojao`-adjacent custom
command image), not tied to any one group, so it has no `group_id` to put
there honestly. `store()` is the raw content-addressed blob layer AGENTS.md
§5 names for exactly this case. Destination keys are content-hash-addressed
under their own `legacy/v1-bucket/` namespace (`keys.py`), which is also what
makes a second run's "content hash already present -> skip" check free: the
key IS the hash.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol

#: Every `list_blobs(prefix=...)` call found in the v1 checkout, in source-file
#: order (`Bot/Miscellaneous.py:16-23`, then `Bot/SocialContent.py:24-25`).
#: Cross-checked against, not replaced by, the set the requester already knew
#: about: `IdeiaDesenho` was not on that list (feeds `/drawingidea`,
#: `Miscellaneous.py:137-143`) and is included here because the source code
#: reads it just as unconditionally as `Death/` or `Fight/English`.
#:
#: Verbatim strings, not normalised with a trailing slash: GCS prefix matching
#: is a byte-prefix match, not a folder-boundary match, and changing e.g.
#: `"Death"` to `"Death/"` would silently narrow what a real run reads
#: compared to what v1 itself reads. Faithfulness to v1's own read pattern is
#: the point (module docstring, "Source of truth").
PREFIXES: tuple[str, ...] = (
    "IdeiaDesenho",  # Miscellaneous.py:16 -- /drawingidea
    "Death",  # Miscellaneous.py:17 -- /death
    "Countdown/BFF",  # Miscellaneous.py:18
    "Countdown/Patas",  # Miscellaneous.py:19
    "Countdown/FurSMeet",  # Miscellaneous.py:20
    "Countdown/Furcamp",  # Miscellaneous.py:21
    "Countdown/Pawstral",  # Miscellaneous.py:22
    # Not read by any v1 code path — and exported anyway, which is the whole
    # point. `Miscellaneous.py:18-22` lists five countdown folders and this is
    # not one of them, so `/trex` has no v1 behaviour behind it; QA specifies
    # the trigger regardless, and `.specs/features/fun_partneredcons/spec.md`
    # concluded from v1's source alone that it "has no image source at all".
    # Listing the real bucket disproved that: `Countdown/Trex` holds 67 images.
    # Exporting them is what turns `/trex` from "invent a pool or drop the
    # trigger" into an ordinary port.
    "Countdown/Trex",
    "Custom/",  # Miscellaneous.py:23,147 -- dynamic per-command subfolders
    "Fight/English",  # SocialContent.py:24 -- /battle
    "Fight/Portuguese",  # SocialContent.py:25 -- /battle
)

Outcome = Literal["copied", "skipped", "failed"]


@dataclass(frozen=True, slots=True)
class SourceBlob:
    """One object as GCS's listing API describes it — metadata only, no bytes."""

    name: str
    size: int
    updated: datetime | None
    md5_hash: str | None


class BucketSource(Protocol):
    """Where blobs come from. Implemented for the real read-only GCS client and,
    in tests, for an in-memory fake — the same split `MongoSource` uses in
    `cb_worker.importer`."""

    def list_prefix(self, prefix: str) -> Iterator[SourceBlob]:
        """Every blob under `prefix`. Must stream, not buffer the whole bucket."""
        ...

    def download(self, name: str) -> bytes:
        """The full bytes of one blob."""
        ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    """One line of the manifest: what happened to one source blob on one run.

    `content_hash` and `destination_key` are `None` for a `"failed"` outcome —
    a blob that could not be downloaded was never hashed.
    """

    prefix: str
    source_path: str
    byte_size: int
    content_hash: str | None
    destination_key: str | None
    outcome: Outcome
    detail: str
    exported_at: str


@dataclass(slots=True)
class PrefixStats:
    """Per-prefix counters — one row of the final summary table."""

    prefix: str
    found: int = 0
    copied: int = 0
    skipped: int = 0
    failed: int = 0
    bytes_copied: int = 0


@dataclass(slots=True)
class ExportReport:
    """What one run did, across every prefix it touched."""

    prefixes: dict[str, PrefixStats] = field(default_factory=dict)
    failures: list[ManifestEntry] = field(default_factory=list)

    def stats_for(self, prefix: str) -> PrefixStats:
        return self.prefixes.setdefault(prefix, PrefixStats(prefix=prefix))

    def total_found(self) -> int:
        return sum(s.found for s in self.prefixes.values())

    def total_copied(self) -> int:
        return sum(s.copied for s in self.prefixes.values())

    def total_skipped(self) -> int:
        return sum(s.skipped for s in self.prefixes.values())

    def total_failed(self) -> int:
        return sum(s.failed for s in self.prefixes.values())

    def total_bytes(self) -> int:
        return sum(s.bytes_copied for s in self.prefixes.values())


@dataclass(frozen=True, slots=True)
class VerifyProblem:
    """One manifest entry that `--verify` could not confirm."""

    source_path: str
    destination_key: str
    status: Literal["missing", "size_mismatch", "hash_mismatch", "error"]
    detail: str


@dataclass(slots=True)
class VerifyReport:
    """What `--verify` found re-reading the manifest against the destination."""

    checked: int = 0
    ok: int = 0
    problems: list[VerifyProblem] = field(default_factory=list)


__all__ = [
    "PREFIXES",
    "BucketSource",
    "ExportReport",
    "ManifestEntry",
    "Outcome",
    "PrefixStats",
    "SourceBlob",
    "VerifyProblem",
    "VerifyReport",
]
