"""Drives one cutover run: preflight, schema, mongo, bucket, memes, verify.

Each step function below is a thin adapter over a tool that already exists and
is already tested on its own (`cb_core.migrations`, `cb_worker.importer.runner`,
`cb_worker.bucket_export.runner`, `cb_worker.meme_seed`) — this module adds
nothing to *what* gets imported or copied, only to how the run is driven,
reported and displayed. A step's own exception never aborts the run: `run_cutover`
catches it, records a `"failed"` `StepResult`, and moves on to the next step,
the same "one bad thing must not cost the rest" contract `bucket_export.runner`
and `importer.runner` already keep one level down (module docstring).

Progress display is deliberately one `rich.progress.Progress` *per step*,
opened and closed within that step's own function, never a `Progress` spanning
the whole run. `bucket`'s own `run_export` already owns a `Progress` internally
(`bucket_export/runner.py`); if this module also held one open across steps,
starting the bucket step would nest a `Live` inside a `Live`, which `rich`
raises `LiveError` for. Running steps strictly sequentially — one `Progress`
context fully closed before the next opens — sidesteps that by construction,
which is also why `mongo` and `memes` below build their own short-lived
`Progress` rather than being handed a shared one.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from urllib.parse import urlsplit

import asyncpg
from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn
from rich.table import Table

from cb_core import db, storage
from cb_core.logging import get_logger
from cb_core.meme_templates import MemeTemplate, all_templates
from cb_core.migrations import ensure_schema, head_revision, migrations_dir
from cb_core.settings import Settings
from cb_core.storage import store_from_uri
from cb_worker import meme_seed
from cb_worker.bucket_export import manifest as bucket_manifest_io
from cb_worker.bucket_export.runner import run_export
from cb_worker.bucket_export.source import open_source as open_bucket_source
from cb_worker.cutover import (
    CheckStatus,
    CutoverReport,
    PreflightCheck,
    StepName,
    StepResult,
    StepStatus,
)
from cb_worker.importer.loader import TABLE_LOADS
from cb_worker.importer.runner import run_import
from cb_worker.importer.source import LiveMongoSource, MongoSourceError
from cb_worker.importer.source import open_source as open_mongo_source

log = get_logger("cb.cutover.runner")

#: Tables the mongo step can write, in the order `loader.TABLE_LOADS` declares
#: them. Reused (not re-listed) for `verify`'s row counts so the two never
#: drift apart — a table the importer starts writing shows up in `verify` for
#: free, a table it stops writing drops out the same way.
_IMPORTED_TABLES: tuple[str, ...] = tuple(TABLE_LOADS)


# --------------------------------------------------------------------- shared


async def _current_revision(dsn: str) -> str | None:
    """The applied alembic revision, or `None` for an empty schema.

    Deliberately not a call into `cb_core.migrations._current_revision` — that
    name is module-private, and reaching into it would make this module depend
    on an implementation detail of another package rather than its public
    surface (`migrations_dir`, `head_revision`, `ensure_schema`). Five lines
    duplicated here is cheaper than that coupling, and it is a read, not a
    second migration mechanism — the only thing that ever applies a revision is
    still `ensure_schema`.
    """
    conn = await asyncpg.connect(dsn=dsn, timeout=5)
    try:
        try:
            return await conn.fetchval("SELECT version_num FROM alembic_version")
        except asyncpg.UndefinedTableError:
            return None
    finally:
        await conn.close()


def _redact_dsn(dsn: str) -> str:
    """host[:port]/database only — never the credentials a DSN carries, for the
    same reason `importer/source.py`'s `_host_for_logging` exists: this string
    ends up in a table printed to a terminal, possibly captured in a CI log."""
    parsed = urlsplit(dsn)
    host = parsed.hostname or "?"
    port = f":{parsed.port}" if parsed.port else ""
    database = parsed.path.lstrip("/") or "?"
    return f"{host}{port}/{database}"


def _bucket_dest_store_options() -> dict[str, str]:
    """Mirrors `bucket_export/__main__.py`'s own `_dest_store_options`: R2 is
    S3-compatible but needs a non-AWS endpoint and region passed through to
    obstore's `S3Store`. Duplicated rather than imported because that function
    is module-private to a sibling `__main__.py` this module otherwise never
    depends on."""
    options: dict[str, str] = {}
    endpoint = os.environ.get("CB_BUCKET_EXPORT_DEST_ENDPOINT", "").strip()
    region = os.environ.get("CB_BUCKET_EXPORT_DEST_REGION", "").strip()
    if endpoint:
        options["endpoint"] = endpoint
    if region:
        options["region"] = region
    return options


def _human_bytes(n: int) -> str:
    """Same rendering `bucket_export.runner._human_bytes` uses, duplicated for
    the same module-private reason as `_bucket_dest_store_options` above."""
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}TB"


# ------------------------------------------------------------------ preflight


async def _check_postgres(settings: Settings) -> PreflightCheck:
    target = _redact_dsn(settings.pg_dsn)
    try:
        conn = await asyncpg.connect(dsn=settings.pg_dsn, timeout=5)
    except Exception as exc:  # noqa: BLE001 - any connection failure is "fail", not a bug here
        return PreflightCheck("postgres", "fail", f"could not reach {target}: {exc}")
    try:
        await conn.fetchval("SELECT 1")
    finally:
        await conn.close()
    return PreflightCheck("postgres", "ok", target)


def _check_storage(settings: Settings, selected: frozenset[StepName]) -> PreflightCheck:
    uri = settings.storage_uri
    write_steps = sorted(selected & {"bucket", "memes"})
    if uri.startswith("memory://"):
        if write_steps:
            # memory:// is per-process: a write step selected against it would
            # "succeed" and then lose every byte the instant this process
            # exits, which reads as a clean run and leaves cutover half done.
            return PreflightCheck(
                "object storage",
                "fail",
                f"CB_STORAGE_URI={uri!r} does not survive this process, but "
                f"{', '.join(write_steps)} would write through it — point "
                "CB_STORAGE_URI at s3://, gs:// or file:// first",
            )
        return PreflightCheck("object storage", "ok", f"{uri} (no write step selected)")
    try:
        built = store_from_uri(uri)
    except Exception as exc:  # noqa: BLE001 - a bad URI is a config error to report, not raise
        return PreflightCheck("object storage", "fail", str(exc))
    bucket = getattr(built, "bucket", None)
    return PreflightCheck("object storage", "ok", f"{built.scheme}://{bucket or uri}")


def _check_mongo(settings: Settings) -> PreflightCheck:
    uri = settings.mongo_uri.strip()
    dump_dir = settings.mongo_dump_dir.strip()
    if not uri and not dump_dir:
        # Absent, not broken: `importer/__init__.py`'s own contract is "both
        # empty means no import is configured, the normal state once cutover
        # is done" — the mongo *step* skips cleanly on this, so preflight must
        # not call it a failure either.
        return PreflightCheck(
            "mongo source", "skip", "neither CB_MONGO_URI nor CB_MONGO_DUMP_DIR is set"
        )
    if uri and dump_dir:
        return PreflightCheck(
            "mongo source", "fail", "both CB_MONGO_URI and CB_MONGO_DUMP_DIR are set"
        )
    if dump_dir:
        path = Path(dump_dir)
        if not path.is_dir():
            return PreflightCheck(
                "mongo source", "fail", f"CB_MONGO_DUMP_DIR={dump_dir} is not a directory"
            )
        return PreflightCheck("mongo source", "ok", f"mongodump directory at {dump_dir}")
    try:
        source = LiveMongoSource(uri, settings.mongo_database, timeout_ms=3000)
        try:
            names = source.collections()
        finally:
            source.close()
    except MongoSourceError as exc:
        return PreflightCheck("mongo source", "fail", str(exc))
    return PreflightCheck("mongo source", "ok", f"{len(names)} collection(s) reachable")


def _check_meme_source(source: Path, selected: frozenset[StepName]) -> PreflightCheck:
    v1_dir = source / meme_seed.V1_SUBPATH
    if not v1_dir.is_dir():
        # Only a hard failure when the memes step is actually about to run —
        # same "absent is not broken" reasoning as `_check_mongo`.
        status: CheckStatus = "fail" if "memes" in selected else "skip"
        return PreflightCheck("meme templates checkout", status, f"{v1_dir} not found")
    return PreflightCheck("meme templates checkout", "ok", str(v1_dir))


async def run_preflight(
    settings: Settings, *, selected: frozenset[StepName], meme_source: Path
) -> list[PreflightCheck]:
    """The four checks cutover day needs before anything writes: Postgres,
    object storage, the Mongo source, and the v1 meme checkout. Every check
    here is a read (`SELECT 1`, a `listCollections`, a directory stat) — never
    a write, per the module's own contract."""
    return [
        await _check_postgres(settings),
        _check_storage(settings, selected),
        _check_mongo(settings),
        _check_meme_source(meme_source, selected),
    ]


def render_preflight(checks: Sequence[PreflightCheck]) -> Table:
    table = Table(title="cutover preflight")
    table.add_column("check")
    table.add_column("status")
    table.add_column("detail")
    style = {"ok": "green", "skip": "yellow", "fail": "bold red"}
    for check in checks:
        table.add_row(check.name, check.status, check.detail, style=style.get(check.status))
    return table


# ------------------------------------------------------------------- schema


async def _step_schema(settings: Settings, *, dry_run: bool) -> StepResult:
    start = time.monotonic()
    directory = migrations_dir(settings)
    head = await asyncio.to_thread(head_revision, directory)
    before = await _current_revision(settings.pg_dsn)

    if dry_run:
        # `command.upgrade(..., sql=True)` would emit the SQL without applying
        # it, but that needs `migrations/env.py`'s offline branch to be exact
        # for every revision — not a risk worth taking on cutover day just to
        # print a preview. Comparing the two revisions says the one thing that
        # actually matters ("is there anything to do") without running alembic
        # at all, which is what "writes nothing" requires.
        headline = (
            "already at head" if before == head else f"would upgrade {before or 'empty'} -> {head}"
        )
        return StepResult(
            step="schema",
            status="ok",
            duration_s=time.monotonic() - start,
            headline=headline,
            detail="dry run: no DDL executed",
        )

    outcome = await ensure_schema(settings)
    if outcome == "disabled":
        return StepResult(
            step="schema",
            status="skipped",
            duration_s=time.monotonic() - start,
            headline="CB_AUTO_MIGRATE=false",
            detail="schema convergence is disabled for this deployment",
        )
    after = await _current_revision(settings.pg_dsn)
    return StepResult(
        step="schema",
        status="ok",
        duration_s=time.monotonic() - start,
        headline=f"{before or 'empty'} -> {after or 'empty'} ({outcome})",
    )


# -------------------------------------------------------------------- mongo


async def _step_mongo(
    settings: Settings, *, collections: Sequence[str] | None, dry_run: bool, console: Console
) -> StepResult:
    start = time.monotonic()
    if not settings.mongo_uri.strip() and not settings.mongo_dump_dir.strip():
        return StepResult(
            step="mongo",
            status="skipped",
            duration_s=time.monotonic() - start,
            headline="no source configured",
            detail="set CB_MONGO_URI or CB_MONGO_DUMP_DIR to run this step",
        )

    source = open_mongo_source(settings)
    try:
        available = source.collections()
        wanted = sorted(set(collections) if collections is not None else set(available))

        with Progress(
            TextColumn("[bold blue]{task.fields[collection]}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=console,
            transient=True,
        ) as progress:
            task_id = progress.add_task("", collection="mongo import", total=len(wanted) or None)

            def on_start(name: str) -> None:
                progress.update(task_id, collection=f"reading {name}")

            def on_done(name: str, read: int, written: int) -> None:
                progress.update(
                    task_id, advance=1, collection=f"{name}: read {read}, wrote {written}"
                )

            report = await run_import(
                source,
                collections=collections,
                dry_run=dry_run,
                batch_size=settings.import_batch_size,
                on_collection_start=on_start,
                on_collection_done=on_done,
            )
    finally:
        source.close()

    # A per-document skip is normal (a bad id, an unmappable value — see
    # `importer/__init__.py`) and never fails the step; a whole-collection
    # `Skipped("*", ...)` means the collection was lost outright, which does.
    collection_failures = [s for s in report.skipped if s.document_id == "*"]
    status: StepStatus = "failed" if collection_failures else "ok"
    return StepResult(
        step="mongo",
        status=status,
        duration_s=time.monotonic() - start,
        headline=f"{report.total_written()} row(s) written",
        detail="; ".join(f"{s.collection}: {s.reason}" for s in collection_failures),
    )


# ------------------------------------------------------------------- bucket


async def _step_bucket(
    settings: Settings, *, dry_run: bool, manifest_path: Path, console: Console
) -> StepResult:
    start = time.monotonic()
    dest_uri = os.environ.get("CB_BUCKET_EXPORT_DEST_URI", "").strip()
    source_bucket = os.environ.get("CB_BUCKET_EXPORT_SOURCE_BUCKET", "").strip()
    if not dest_uri or not source_bucket:
        return StepResult(
            step="bucket",
            status="skipped",
            duration_s=time.monotonic() - start,
            headline="not configured",
            detail=(
                "set CB_BUCKET_EXPORT_DEST_URI and CB_BUCKET_EXPORT_SOURCE_BUCKET to run this step"
            ),
        )

    store = store_from_uri(dest_uri, **_bucket_dest_store_options())
    try:
        source = open_bucket_source(source_bucket)
        try:
            # `run_export` owns its own `rich.progress.Progress` (see
            # `bucket_export/runner.py`) — handing it this step's `console`
            # reuses that display exactly as it already renders standalone,
            # per the module docstring's "one Progress per step" contract.
            export_report = await run_export(
                source,
                store,
                manifest_path=manifest_path,
                dry_run=dry_run,
                console=console,
            )
        finally:
            source.close()
    finally:
        await store.close()

    status: StepStatus = "ok" if export_report.total_failed() == 0 else "failed"
    return StepResult(
        step="bucket",
        status=status,
        duration_s=time.monotonic() - start,
        headline=(
            f"{export_report.total_copied()} object(s) copied, "
            f"{export_report.total_skipped()} skipped"
        ),
        detail=f"{export_report.total_failed()} failed" if export_report.total_failed() else "",
    )


# -------------------------------------------------------------------- memes


async def _step_memes(
    settings: Settings, *, source: Path, dry_run: bool, console: Console
) -> StepResult:
    start = time.monotonic()
    if settings.storage_uri.startswith("memory://") and not dry_run:
        # Same refusal `meme_seed.py`'s own CLI makes — a memory store loses
        # every byte the instant this process exits, so writing to it here
        # would look like a successful seed and leave the feature dead.
        return StepResult(
            step="memes",
            status="failed",
            duration_s=time.monotonic() - start,
            headline="storage is memory://",
            detail="CB_STORAGE_URI is memory://; point it at s3://, gs:// or file:// first",
        )

    templates = all_templates()
    with Progress(
        TextColumn("[bold blue]{task.fields[template]}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as progress:
        task_id = progress.add_task("", template="meme templates", total=len(templates))

        def on_template(template: MemeTemplate, outcome: str) -> None:
            progress.update(task_id, advance=1, template=f"{template.filename} ({outcome})")

        report = await meme_seed.seed(source, dry_run=dry_run, on_template=on_template)

    status: StepStatus = "ok" if report.ok else "failed"
    detail = ""
    if not report.ok:
        detail = f"{len(report.missing or [])} missing, {len(report.failed or [])} failed"
    return StepResult(
        step="memes",
        status=status,
        duration_s=time.monotonic() - start,
        headline=f"{report.copied} copied, {report.skipped} skipped",
        detail=detail,
    )


# ------------------------------------------------------------------- verify


def render_verify(
    rows: Mapping[str, int],
    revision: str | None,
    meme_present: int,
    meme_total: int,
    bucket_objects: int,
    bucket_bytes: int,
    bucket_note: str,
) -> Table:
    table = Table(title="cutover verify")
    table.add_column("item")
    table.add_column("value", justify="right")
    for name, count in rows.items():
        table.add_row(f"rows: {name}", str(count))
    table.add_section()
    table.add_row("alembic revision", revision or "empty")
    table.add_row("meme templates present", f"{meme_present}/{meme_total}")
    table.add_row(
        "bucket export objects",
        f"{bucket_objects} ({_human_bytes(bucket_bytes)}) — {bucket_note}",
    )
    return table


async def _step_verify(settings: Settings, *, console: Console, manifest_path: Path) -> StepResult:
    """Read-only, always: row counts per table the mongo step can write, the
    object counts the bucket and memes steps landed, and the alembic revision.
    Safe to run on its own (`--only verify`) at any point in a cutover, before
    or after the other steps, which is the point of it being read-only."""
    start = time.monotonic()

    rows: dict[str, int] = {}
    for table in _IMPORTED_TABLES:
        # Table names come only from `TABLE_LOADS`, never from user input — the
        # same "not a second SQL-building path" reasoning `loader._upsert_sql`
        # documents for its own f-string.
        row = await db.fetchrow(
            f"SELECT count(*) AS n FROM {table}", name=f"cutover_verify_{table}"
        )
        rows[table] = int(row["n"]) if row is not None else 0

    revision = await _current_revision(settings.pg_dsn)

    store = storage.store()
    templates = all_templates()
    meme_present = 0
    for template in templates:
        if await store.exists(template.storage_key):
            meme_present += 1

    bucket_objects = 0
    bucket_bytes = 0
    bucket_note = "no bucket export manifest found — the bucket step has not run yet"
    if bucket_manifest_io.path_exists(manifest_path):
        landed = [
            entry
            for entry in bucket_manifest_io.latest_by_source(manifest_path).values()
            if entry.outcome in ("copied", "skipped")
        ]
        bucket_objects = len(landed)
        bucket_bytes = sum(entry.byte_size for entry in landed)
        bucket_note = f"from manifest at {manifest_path}"

    console.print(
        render_verify(
            rows, revision, meme_present, len(templates), bucket_objects, bucket_bytes, bucket_note
        )
    )

    return StepResult(
        step="verify",
        status="ok",
        duration_s=time.monotonic() - start,
        headline=f"{sum(rows.values())} row(s), revision {revision or 'empty'}",
    )


# ---------------------------------------------------------------- preflight step


async def _step_preflight(
    settings: Settings,
    *,
    selected: frozenset[StepName],
    meme_source: Path,
    console: Console,
    report: CutoverReport,
) -> StepResult:
    start = time.monotonic()
    checks = await run_preflight(settings, selected=selected, meme_source=meme_source)
    report.preflight_checks = checks
    console.print(render_preflight(checks))

    failed = [c for c in checks if c.status == "fail"]
    status: StepStatus = "failed" if failed else "ok"
    ok_count = sum(1 for c in checks if c.status == "ok")
    return StepResult(
        step="preflight",
        status=status,
        duration_s=time.monotonic() - start,
        headline=f"{ok_count}/{len(checks)} ok",
        detail="; ".join(f"{c.name}: {c.detail}" for c in failed),
    )


# --------------------------------------------------------------------- summary


def render_summary(report: CutoverReport) -> Table:
    """step, status, duration, and the one number that matters for that step —
    in the order the steps actually ran, which is `STEP_ORDER` filtered by
    whatever `--only`/`--skip` selected."""
    table = Table(title="cutover summary")
    table.add_column("step")
    table.add_column("status")
    table.add_column("duration", justify="right")
    table.add_column("result")
    style = {"ok": "green", "skipped": "yellow", "failed": "bold red"}
    for result in report.steps:
        table.add_row(
            result.step,
            result.status,
            f"{result.duration_s:.1f}s",
            result.headline,
            style=style.get(result.status),
        )
    return table


# ----------------------------------------------------------------------- run


#: Steps that read or write through `cb_core.db`'s shared pool rather than a
#: connection of their own. `schema` is deliberately absent: `ensure_schema`
#: and `_current_revision` each open a plain `asyncpg.connect` (mirroring how
#: `cb_core.migrations` already does this at service startup), so a schema
#: step's own failure already surfaces through the per-step `try`/`except`
#: below without this module needing to open the pool at all for it.
_DB_STEPS = frozenset({"mongo", "verify"})
_STORAGE_STEPS = frozenset({"bucket", "memes", "verify"})


def _infra_problem(
    step: StepName, *, dry_run: bool, db_error: str | None, storage_error: str | None
) -> str | None:
    """Whether `step` needs infrastructure that failed to initialise — and if
    so, why. `None` means the step can proceed to its own body."""
    if db_error is not None and step in _DB_STEPS and not (step == "mongo" and dry_run):
        return db_error
    if storage_error is not None and step in _STORAGE_STEPS:
        return storage_error
    return None


async def run_cutover(
    settings: Settings,
    steps: Sequence[StepName],
    *,
    dry_run: bool,
    collections: Sequence[str] | None,
    memes_source: Path,
    bucket_manifest_path: Path,
    console: Console,
) -> CutoverReport:
    """Run every selected step, in `STEP_ORDER`, catching each one's own
    exception so a failure never costs the steps after it (module docstring).

    The Postgres pool and the object-store client are each initialised once,
    only if a selected step actually needs them, and closed once at the end —
    not per step — because `mongo` and `verify` share the pool and `bucket`,
    `memes` and `verify` share the store, and a per-step init/close would
    reopen a connection pool for no reason between two steps that both want it.

    Mongo's own writes only happen when `dry_run` is false (`run_import`'s
    dry-run path never calls `cb_core.db`), so the pool is not opened for a
    dry-run mongo-only selection — a `--dry-run --only mongo` must still work
    with no reachable Postgres at all, which is what `--dry-run --only
    preflight,verify` also has to survive for `verify`'s own, unconditional
    read. Either way, a pool or store that fails to open is *not* raised here:
    it is recorded and turned into a `"failed"` `StepResult` for exactly the
    steps that needed it, so an unreachable Postgres degrades one row of the
    summary table instead of crashing the whole run before it prints anything.
    """
    report = CutoverReport()
    selected = frozenset(steps)

    needs_db = "verify" in selected or ("mongo" in selected and not dry_run)
    needs_storage = bool(selected & _STORAGE_STEPS)

    db_error: str | None = None
    storage_error: str | None = None

    if needs_db:
        try:
            await db.init_pool(
                settings.model_copy(update={"pg_command_timeout": settings.import_command_timeout})
            )
        except Exception as exc:  # noqa: BLE001 - degrade to per-step failures, see docstring
            db_error = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            log.warning("cutover.db_pool.unavailable", error=db_error)

    if needs_storage:
        try:
            await storage.init_storage(settings)
        except Exception as exc:  # noqa: BLE001 - same degrade-not-crash contract as the pool above
            storage_error = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            log.warning("cutover.storage.unavailable", error=storage_error)

    try:
        for step in steps:
            start = time.monotonic()
            infra_problem = _infra_problem(
                step, dry_run=dry_run, db_error=db_error, storage_error=storage_error
            )
            if infra_problem is not None:
                result = StepResult(
                    step=step,
                    status="failed",
                    duration_s=0.0,
                    headline="infrastructure unavailable",
                    detail=infra_problem,
                )
                report.steps.append(result)
                continue

            try:
                result = await _run_step(
                    step,
                    settings,
                    selected=selected,
                    dry_run=dry_run,
                    collections=collections,
                    memes_source=memes_source,
                    bucket_manifest_path=bucket_manifest_path,
                    console=console,
                    report=report,
                )
            except Exception as exc:
                log.exception("cutover.step.failed", step=step)
                detail = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
                result = StepResult(
                    step=step,
                    status="failed",
                    duration_s=time.monotonic() - start,
                    headline="raised an exception",
                    detail=detail,
                )
            report.steps.append(result)
    finally:
        if needs_storage:
            await storage.close_storage()
        if needs_db:
            await db.close_pool()

    return report


async def _run_step(
    step: StepName,
    settings: Settings,
    *,
    selected: frozenset[StepName],
    dry_run: bool,
    collections: Sequence[str] | None,
    memes_source: Path,
    bucket_manifest_path: Path,
    console: Console,
    report: CutoverReport,
) -> StepResult:
    if step == "preflight":
        return await _step_preflight(
            settings, selected=selected, meme_source=memes_source, console=console, report=report
        )
    if step == "schema":
        return await _step_schema(settings, dry_run=dry_run)
    if step == "mongo":
        return await _step_mongo(
            settings, collections=collections, dry_run=dry_run, console=console
        )
    if step == "bucket":
        return await _step_bucket(
            settings, dry_run=dry_run, manifest_path=bucket_manifest_path, console=console
        )
    if step == "memes":
        return await _step_memes(settings, source=memes_source, dry_run=dry_run, console=console)
    if step == "verify":
        return await _step_verify(settings, console=console, manifest_path=bucket_manifest_path)
    raise AssertionError(f"unhandled cutover step {step!r}")  # StepName is exhaustive above


__all__ = [
    "render_preflight",
    "render_summary",
    "render_verify",
    "run_cutover",
    "run_preflight",
]
