"""v1's private-bucket cutover export, as a frozen index — `meme_templates.py`'s
counterpart for `cb_worker.bucket_export`'s 6,900-odd objects.

`cb_worker.bucket_export` copies every blob under v1's `PREFIXES`
(`cb_worker/bucket_export/__init__.py`) into `cb_core.storage` under a
content-addressed key (`legacy/v1-bucket/<hh>/<hash><ext>`) — the right shape
for storage, since identical bytes land once regardless of which v1 folder
they came from, but it throws away the one thing a feature like `/death` or
`/battle` needs to pick from *its own* pool: which v1 prefix a given blob
belonged to. That mapping survives in the export manifest
(`cb_worker/bucket_export/manifest.py`), and `python scripts/cb.py
legacy-catalog` (`cb_worker/bucket_export/catalog.py`) turns it into the small
per-prefix CSV catalogs this module reads — the same split
`meme_templates.py`'s own docstring describes for `meme_metadata.csv`: a tiny
catalog ships as package data, the bytes it describes live in
`cb_core.storage`, and both halves have to agree on the key rather than
re-deriving it.

**What "agree on the key" means here isn't a storage key** — every row's
`destination_key` already *is* the content-addressed storage key
`bucket_export.keys.destination_key` computed at export time, and
`LegacyAsset.storage_key` below just exposes it (mirroring
`MemeTemplate.storage_key`'s role, even though nothing is derived). What the
writer and this reader do have to agree on is **the mapping from a v1 prefix
to an on-disk catalog filename** — `catalog_relpath` below, imported by
`cb_worker.bucket_export.catalog` rather than redefined there, so the two
halves can never disagree about where a given prefix's rows live. It lives
here, in `cb-core`, rather than in `cb-worker`, for the same layering reason
`bucket_export.keys` cannot live here: `cb-worker` already depends on
`cb-core`, never the other way, and this mapping has to be reachable from
both the generator (`cb-worker`) and every future consumer (`cb-gateway`
handlers, which never depend on `cb-worker` either).

Everything is loaded once at import into immutable structures, same
discipline `meme_templates.py` and `locales.py` both use: no I/O and no cache
invalidation on the request path. **A deployment that has not run
`legacy-catalog` yet is not an error** — the `cb_core.asset_data.legacy`
package itself always ships (its `__init__.py` is checked in), but the CSV
catalogs inside it are generated artifacts nobody has run `legacy-catalog`
against yet in a fresh checkout. `prefixes()`, `entries_for()` and
`custom_command_names()` all degrade to empty on that missing input, and
`choose()` returns `None` for an empty pool — the same "no bytes seeded yet"
degradation `meme_templates.choose` documents for its own empty-pool case,
not an `ImportError` that would take a whole deployment down over one
un-run cutover step.
"""

from __future__ import annotations

import csv
import random
from collections.abc import Iterator
from importlib import resources
from importlib.resources.abc import Traversable
from types import MappingProxyType

import msgspec

_DATA_PACKAGE = "cb_core.asset_data.legacy"

#: The on-disk directory (and catalog-key prefix) `Custom/<command>` rows land
#: under — lowercase because `catalog_relpath` lowercases every path segment;
#: see that function's own docstring for why casing is not preserved on disk.
_CUSTOM_DIRNAME = "custom"


class LegacyAsset(msgspec.Struct, frozen=True):
    """One row of a generated catalog — one blob `bucket_export` already
    landed in `cb_core.storage`, described with just enough to pick it and
    trace it back to v1: nothing a consumer does not need (task description's
    own "what a consumer needs and nothing more"), which is why this is not
    simply `ManifestEntry` reused — that type also carries `prefix`,
    `outcome`, `detail` and `exported_at`, none of which a feature handler
    has any use for once the catalog has already been filtered and grouped.
    """

    source_path: str
    destination_key: str
    byte_size: int
    content_hash: str

    @property
    def storage_key(self) -> str:
        """Where the bytes live in `cb_core.storage`. `destination_key` is
        already the content-addressed key `bucket_export.keys.destination_key`
        computed at export time — this property adds no derivation, only a
        single named accessor every consumer goes through instead of reaching
        for `.destination_key` by field name, mirroring
        `MemeTemplate.storage_key`'s role (module docstring).
        """
        return self.destination_key


def catalog_relpath(key: str) -> str:
    """A v1 prefix (`"Countdown/BFF"`) or a `"Custom/<command>"` grouping key
    -> the on-disk catalog filename, relative to `cb_core.asset_data.legacy`.

    Slash-separated segments become directory nesting (`"Countdown/BFF"` ->
    `"countdown/bff.csv"`, `"Fight/English"` -> `"fight/english.csv"`), and
    every segment is lowercased. That lowercasing is a filesystem convention
    only — it avoids ever depending on a case-sensitive filesystem to tell
    `Death` and `death` apart — not a claim about v1's own casing, which
    survives byte-for-byte in every row's `source_path` instead. Shared by
    the writer (`cb_worker.bucket_export.catalog`) and this reader so the two
    can never derive a different filename for the same prefix (module
    docstring's "agree on the key").
    """
    segments = [segment.lower() for segment in key.split("/") if segment]
    if not segments:
        raise ValueError(f"empty catalog key: {key!r}")
    return "/".join(segments) + ".csv"


def _walk_csv_files(
    node: Traversable, path_segments: tuple[str, ...]
) -> Iterator[tuple[str, Traversable]]:
    """Every `*.csv` file under `node`, paired with its catalog key (the
    relpath with the extension stripped, e.g. `"countdown/bff"`).

    Recursive rather than hardcoded to `PREFIXES`' current two-level depth
    (`Countdown/BFF`, `Custom/<command>`) on purpose: this module has no
    business assuming how deep a v1 prefix nests, only `bucket_export`
    (`cb-worker`) knows `PREFIXES`, and a future prefix one level deeper must
    not require a matching change here.
    """
    if not node.is_dir():
        return
    for entry in sorted(node.iterdir(), key=lambda item: item.name):
        if entry.is_dir():
            yield from _walk_csv_files(entry, (*path_segments, entry.name))
        elif entry.is_file() and entry.name.endswith(".csv"):
            key = "/".join((*path_segments, entry.name[: -len(".csv")]))
            yield key, entry


def _load_catalog(traversable: Traversable) -> tuple[LegacyAsset, ...]:
    raw = traversable.read_text(encoding="utf-8")
    return tuple(
        LegacyAsset(
            source_path=row["source_path"],
            destination_key=row["destination_key"],
            byte_size=int(row["byte_size"]),
            content_hash=row["content_hash"],
        )
        for row in csv.DictReader(raw.splitlines())
    )


def _load() -> dict[str, tuple[LegacyAsset, ...]]:
    try:
        root = resources.files(_DATA_PACKAGE)
    except ModuleNotFoundError:
        # The package itself is checked in (its `__init__.py` ships
        # unconditionally, module docstring), so this branch is a belt for a
        # packaging accident, not a path a normal build ever takes.
        return {}
    return {key: _load_catalog(traversable) for key, traversable in _walk_csv_files(root, ())}


_INDEX: MappingProxyType[str, tuple[LegacyAsset, ...]] = MappingProxyType(_load())


def _choose(pool: tuple[LegacyAsset, ...], rng: random.Random | None) -> LegacyAsset | None:
    if not pool:
        return None
    picker = rng.choice if rng is not None else random.choice
    return picker(list(pool))


def prefixes() -> tuple[str, ...]:
    """Every static (non-`Custom/`) catalog actually shipped, e.g.
    `("countdown/bff", "death", "fight/english", "ideiadesenho", ...)`.

    Empty on a deployment where `legacy-catalog` has never run (module
    docstring) — callers should treat that exactly like `meme_templates`'s
    empty catalog: a legitimate, if unfinished, deployment state, not a bug
    to special-case.
    """
    return tuple(sorted(key for key in _INDEX if not key.startswith(f"{_CUSTOM_DIRNAME}/")))


def entries_for(prefix: str) -> tuple[LegacyAsset, ...]:
    """Every row of `prefix`'s catalog, or `()` if that catalog was never
    generated (or never had rows). Case-insensitive so a caller can pass the
    same literal v1 prefix string `PREFIXES` uses (`"Countdown/BFF"`,
    `"Death"`) without first lowercasing it by hand.
    """
    return _INDEX.get(prefix.lower(), ())


def choose(prefix: str, rng: random.Random | None = None) -> LegacyAsset | None:
    """One entry at random from `entries_for(prefix)`, or `None` for an empty
    pool — mirrors `meme_templates.choose`'s contract exactly, including the
    injectable `rng` so a test can seed it and the `None` return instead of
    v1's own `random.randint(0, -1)` crash on an empty bucket listing
    (see e.g. `.specs/features/fun_death/spec.md`'s D-DE-3).
    """
    return _choose(entries_for(prefix), rng)


def custom_command_names() -> tuple[str, ...]:
    """Every `Custom/<command>` sub-folder a catalog was generated for.

    v1 discovers these once, at process start, by listing the `Custom/`
    prefix and taking the first path segment after it:
    `custom_commands = list(dict.fromkeys([folder.name.split('/')[1] for
    folder in storage_bucket.list_blobs(prefix="Custom/")]))`
    (`Miscellaneous.py:23`) — a case-preserving list in GCS listing order,
    deduplicated by first appearance. This function's names come from the
    same underlying listing (`legacy-catalog` walked it once, at generation
    time, instead of v1's own process doing it on every restart), lowercased
    (module docstring's filesystem-casing note) and returned sorted rather
    than in listing order: GCS listing order is an accident of storage, not a
    behaviour any caller should depend on, and a stable order is what makes
    this function's return value reproducible across two catalog builds.
    """
    dir_prefix = f"{_CUSTOM_DIRNAME}/"
    return tuple(sorted(key[len(dir_prefix) :] for key in _INDEX if key.startswith(dir_prefix)))


def entries_for_custom(name: str) -> tuple[LegacyAsset, ...]:
    """Every row for one custom command's folder, or `()` if that command has
    no generated catalog. `name` is matched case-insensitively, same as
    `entries_for` — v1's own dispatch already lowercases/normalises command
    names before this point (`textmatch.py`), so this mirrors what a caller
    already has in hand rather than forcing it to re-derive the exact on-disk
    casing.
    """
    return _INDEX.get(f"{_CUSTOM_DIRNAME}/{name.lower()}", ())


def choose_custom(name: str, rng: random.Random | None = None) -> LegacyAsset | None:
    """`choose`'s counterpart for a `Custom/<command>` pool — same contract,
    same `None`-on-empty, same injectable `rng`."""
    return _choose(entries_for_custom(name), rng)


__all__ = [
    "LegacyAsset",
    "catalog_relpath",
    "choose",
    "choose_custom",
    "custom_command_names",
    "entries_for",
    "entries_for_custom",
    "prefixes",
]
