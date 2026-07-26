"""Drives one import run: read every requested collection, map it, load it.

This is the layer that owns *order*. Two ordering decisions matter and both
exist to satisfy the foreign key from `group_configs`/`group_rules`/
`group_welcomes`/`group_admins` to `groups`, without ever letting one missing
document abort the run:

1. Collections are visited in `_COLLECTION_ORDER`, so `groups` (which is what
   populates real `groups` rows, title/image_url included) is read before
   `configs`/`rules`/`welcomes` when all four are present in the same run.
2. That alone is not enough — a run can be asked for `--collections configs`
   only, or v1's Mongo can simply have a `configs` document for a chat the
   `groups` collection never recorded. So immediately before any table with an
   FK to `groups` is written, `loader.ensure_group_stubs` inserts a bare
   `groups` row (`ON CONFLICT DO NOTHING`) for every `group_id` about to be
   referenced. That is what actually guarantees the child insert cannot hit a
   foreign-key violation, regardless of source order; step 1 just keeps the
   stub path cold in the common case.

`dry_run=True` still reads and maps everything — and still runs `Skipped`
accounting through the mapper — it only skips `ensure_group_stubs` and
`load_rows`, counting the rows each table *would* have received instead.

A collection that raises (a bad document the mapper didn't catch, a database
error mid-batch) is caught here, logged, and recorded as a whole-collection
`Skipped` entry; the loop moves on so one bad collection never loses the report
for the others, per `importer/__init__.py`'s contract.
"""

from __future__ import annotations

from collections.abc import Sequence

from cb_core.logging import get_logger
from cb_worker.importer import ImportReport, MappedRows, MongoSource, Skipped
from cb_worker.importer.loader import TABLE_LOADS, ensure_group_stubs, load_rows
from cb_worker.importer.mappers import MAPPERS

log = get_logger("cb.importer.runner")

#: Tables an FK ties to `groups(group_id)` — checked with `ensure_group_stubs`
#: before their rows are written, no matter which collection produced them.
_GROUP_FK_TABLES = frozenset({"group_configs", "group_rules", "group_welcomes", "group_admins"})

#: Write order for the tables a *single* collection's mapper can populate.
#: `groups` first matters within one collection's own batch: a `groups`
#: document's mapper writes both `groups` and `group_admins` rows (per
#: `importer/__init__.py`'s docstring), and `group_admins` carries the same FK,
#: so that one batch must write `groups` before `group_admins` even though
#: `ensure_group_stubs` would also make it safe.
_TABLE_WRITE_ORDER: tuple[str, ...] = (
    "groups",
    "group_configs",
    "group_rules",
    "group_welcomes",
    "group_admins",
    "users",
    "blacklist",
)

#: Collection read order. `groups` first so real `groups` rows (with title and
#: image_url) tend to exist before `configs`/`rules`/`welcomes` need a `groups`
#: row at all — belt alongside `ensure_group_stubs`'s braces, not a substitute.
_COLLECTION_ORDER: tuple[str, ...] = (
    "groups",
    "configs",
    "rules",
    "welcomes",
    "users",
    "blacklist",
)


def _ordered(available: Sequence[str], wanted: set[str]) -> list[str]:
    """`available` filtered to `wanted`, in `_COLLECTION_ORDER`, unknowns last."""
    selected = [c for c in available if c in wanted]
    known = [c for c in _COLLECTION_ORDER if c in selected]
    known.extend(c for c in selected if c not in _COLLECTION_ORDER)
    return known


async def run_import(
    source: MongoSource,
    *,
    collections: Sequence[str] | None = None,
    dry_run: bool = False,
    batch_size: int = 500,
) -> ImportReport:
    """Read, map and load every requested collection; always return a full report.

    `collections=None` means every collection `source.collections()` offers.
    Idempotent end to end: every table write below is an `ON CONFLICT` upsert
    (`loader.TABLE_LOADS`), so running this twice in a row — or once against a
    live v1 database and again at cutover — produces the same rows, not
    duplicates or resurrected defaults (see `loader.py`'s module docstring for
    exactly which columns each re-run overwrites).
    """
    report = ImportReport()
    available = source.collections()
    wanted = set(collections) if collections is not None else set(available)

    for name in sorted(wanted - set(available)):
        log.warning("import.collection.unavailable", collection=name)

    for collection in _ordered(available, wanted):
        try:
            await _import_collection(
                collection, source, report, dry_run=dry_run, batch_size=batch_size
            )
        except Exception as exc:
            log.exception("import.collection.failed", collection=collection)
            # The type name, not just str(exc): `TimeoutError()` stringifies to
            # "" and produced the report line "collection failed: " — which says
            # a collection was lost without saying anything about why.
            detail = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            report.skipped.append(Skipped(collection, "*", f"collection failed: {detail}"))

    return report


async def _import_collection(
    collection: str,
    source: MongoSource,
    report: ImportReport,
    *,
    dry_run: bool,
    batch_size: int,
) -> None:
    mapper = MAPPERS.get(collection)
    if mapper is None:
        log.warning("import.collection.no_mapper", collection=collection)
        return

    mapped = MappedRows()
    read = 0
    for document in source.read(collection):
        read += 1
        mapper(document, mapped)
    report.read[collection] = read
    report.skipped.extend(mapped.skipped)

    # A mapper producing a table `loader.py` does not know how to write is a
    # wiring bug between the two layers, not bad input data — fail loudly before
    # writing anything, rather than silently dropping rows dry-run wouldn't have
    # caught either.
    unknown_tables = set(mapped.rows) - set(TABLE_LOADS)
    if unknown_tables:
        raise ValueError(
            f"mapper for {collection!r} produced unregistered table(s): {sorted(unknown_tables)}"
        )

    if dry_run:
        for table, rows in mapped.rows.items():
            report.written[table] = report.written.get(table, 0) + len(rows)
        log.info("import.collection.done", collection=collection, read=read, dry_run=True)
        return

    for table in _TABLE_WRITE_ORDER:
        table_rows = mapped.rows.get(table)
        if not table_rows:
            continue
        if table in _GROUP_FK_TABLES:
            # group_id is column 0 of every FK-bearing row by the convention
            # documented in loader.py.
            await ensure_group_stubs({row[0] for row in table_rows})
        written = await load_rows(table, table_rows, batch_size=batch_size)
        report.written[table] = report.written.get(table, 0) + written

    log.info("import.collection.done", collection=collection, read=read, dry_run=False)
