"""Schema convergence at startup.

Every service calls `ensure_schema()` after the pool is up, so a process never
runs against a schema older than the code it ships. v1 had no migration
mechanism at all — the Mongo collections were whatever the last deploy happened
to write — and the Java service and the Python bot disagreed about field names
more than once (FEATURE-MAP §5).

Three properties matter here:

* **Cheap when there is nothing to do.** The common case is one `SELECT` against
  `alembic_version`; alembic is not even imported until a revision is missing.
* **Safe with N replicas.** Whoever wins a session-level advisory lock migrates;
  the rest wait on the lock and then find themselves already at head. Two
  processes never run `upgrade` concurrently.
* **Fatal when it fails.** A service that cannot reach head must not start and
  begin serving against a half-built schema. Set `CB_AUTO_MIGRATE=false` where a
  separate migration job owns the schema (the lock makes that safe either way).

The alembic run itself is synchronous (psycopg + SQLAlchemy, as `migrations/env.py`
wants) so it goes to a thread. Startup is the only place that is acceptable.
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import asyncpg

from cb_core.logging import get_logger
from cb_core.settings import Settings

log = get_logger("cb.migrations")

# Any bigint works; it only has to be the same in every process. Namespaced by
# being an arbitrary constant nothing else in this system uses.
_LOCK_KEY = 0xC00C1EB07


class MigrationError(RuntimeError):
    """Raised when the schema cannot be brought to head."""


# ------------------------------------------------------------------ locating the revisions


def _candidate_dirs() -> list[Path]:
    """Where `migrations/` can be, most authoritative first.

    The revisions live in `packages/cb-api/migrations`, outside any importable
    package, because alembic wants them next to `alembic.ini`. That means they
    cannot simply be imported, so: ask for `cb_api`'s location (correct for the
    editable workspace install every service uses), then fall back to walking up
    from the process CWD and from this file for a source checkout.
    """
    seen: list[Path] = []

    try:
        spec = importlib.util.find_spec("cb_api")
    except (ImportError, ValueError):  # not installed in this image
        spec = None
    if spec is not None:
        # cb_api is a namespace package (no __init__.py), so read the search
        # locations rather than spec.origin, which is None for such packages.
        # .../packages/cb-api/src/cb_api -> .../packages/cb-api
        for location in spec.submodule_search_locations or []:
            seen.append(Path(location).resolve().parents[1] / "migrations")

    for start in (Path.cwd().resolve(), Path(__file__).resolve()):
        for parent in (start, *start.parents):
            seen.append(parent / "packages" / "cb-api" / "migrations")

    return seen


def migrations_dir(settings: Settings) -> Path:
    """The alembic script location, or raise saying exactly what was tried."""
    if settings.migrations_dir:
        explicit = Path(settings.migrations_dir).expanduser().resolve()
        if not (explicit / "versions").is_dir():
            raise MigrationError(f"CB_MIGRATIONS_DIR={explicit} has no versions/ directory")
        return explicit

    tried: list[Path] = []
    for candidate in _candidate_dirs():
        if candidate in tried:
            continue
        tried.append(candidate)
        if (candidate / "versions").is_dir():
            return candidate.resolve()

    raise MigrationError(
        "could not locate the alembic migrations directory; set CB_MIGRATIONS_DIR. Tried: "
        + ", ".join(str(p) for p in tried[:5])
    )


def _quiet_alembic_plugins() -> None:
    """Drop "setup plugin alembic.autogenerate.*", emitted once per plugin the
    first time alembic is touched. `alembic.runtime.migration` stays at INFO — it
    names the revisions that ran, which is what you want in a startup log.

    Called before the first alembic import, because that is when it fires.
    """
    import logging

    logging.getLogger("alembic.runtime.plugins").setLevel(logging.WARNING)


def head_revision(directory: Path) -> str:
    """The single revision `upgrade head` would land on."""
    _quiet_alembic_plugins()

    from alembic.script import ScriptDirectory

    heads = ScriptDirectory(str(directory)).get_heads()
    if len(heads) != 1:
        raise MigrationError(
            f"expected exactly one head in {directory}, found {len(heads)}: {sorted(heads)}"
        )
    return heads[0]


# ------------------------------------------------------------------------------ the upgrade


def _sqlalchemy_url(dsn: str) -> str:
    """asyncpg DSN -> the psycopg URL SQLAlchemy needs. Mirrors migrations/env.py."""
    if dsn.startswith("postgresql://"):
        return dsn.replace("postgresql://", "postgresql+psycopg://", 1)
    return dsn


def _upgrade_head(dsn: str, directory: Path) -> None:
    """Synchronous alembic run — always called through `asyncio.to_thread`."""
    _quiet_alembic_plugins()

    from alembic import command
    from alembic.config import Config

    cfg = Config()  # no ini file: nothing in it applies outside the CLI
    cfg.set_main_option("script_location", str(directory))
    cfg.set_main_option("sqlalchemy.url", _sqlalchemy_url(dsn))
    command.upgrade(cfg, "head")


async def _current_revision(conn: asyncpg.Connection) -> str | None:
    """The applied revision, or None when the schema is empty.

    A missing `alembic_version` table is the first-boot case, not an error.
    """
    try:
        return await conn.fetchval("SELECT version_num FROM alembic_version")
    except asyncpg.UndefinedTableError:
        return None


async def ensure_schema(settings: Settings) -> str:
    """Bring the database to head if it is not there already.

    Returns what happened: `disabled`, `current`, `upgraded`, or `converged`
    (a peer replica did the work while this process waited on the lock).
    """
    if not settings.auto_migrate:
        log.info("schema.auto_migrate.disabled")
        return "disabled"

    directory = migrations_dir(settings)
    head = await asyncio.to_thread(head_revision, directory)

    # Fast path: one query, no lock, no alembic import.
    conn = await asyncpg.connect(dsn=settings.pg_dsn)
    try:
        current = await _current_revision(conn)
        if current == head:
            log.debug("schema.current", revision=head)
            return "current"

        log.info("schema.behind", current=current, head=head, dir=str(directory))
        try:
            await asyncio.wait_for(
                conn.execute("SELECT pg_advisory_lock($1)", _LOCK_KEY),
                timeout=settings.migrate_lock_timeout,
            )
        except TimeoutError as exc:
            raise MigrationError(
                f"waited {settings.migrate_lock_timeout}s for the migration lock; "
                "another process is still migrating"
            ) from exc

        # Recheck under the lock: whoever held it before us may have been a peer
        # replica doing exactly this work.
        if await _current_revision(conn) == head:
            log.info("schema.converged", revision=head)
            return "converged"

        await asyncio.to_thread(_upgrade_head, settings.pg_dsn, directory)

        applied = await _current_revision(conn)
        if applied != head:
            raise MigrationError(f"upgrade finished at {applied!r}, expected {head!r}")
        log.info("schema.upgraded", previous=current or "empty", revision=head)
        return "upgraded"
    finally:
        # Closing releases the session-level advisory lock; no explicit unlock,
        # so a crash mid-upgrade cannot leave the lock held.
        await conn.close()
