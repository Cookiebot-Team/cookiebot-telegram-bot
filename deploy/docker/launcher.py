"""One entrypoint for every deployable.

`scripts/cb.py` shells out to `uv run granian ...`, which is right for a laptop
and wrong for a container: it needs uv, the lockfile and the workspace present at
runtime. This module starts the same servers in-process from an installed
environment, so the runtime image carries no build tooling.

It is also what the Nuitka build compiles: a single module with static imports
that a compiler can actually follow.

    CB_SERVICE=cb-gateway python -m cb_launcher     # granian, ASGI, :8081
    CB_SERVICE=cb-api     python -m cb_launcher     # granian, ASGI, :8000
    CB_SERVICE=cb-worker  python -m cb_launcher     # arq consumer
    CB_SERVICE=migrate    python -m cb_launcher     # alembic upgrade head, then exit
"""

from __future__ import annotations

import os
import sys

# host/port default to the values scripts/cb.py uses, so a container and a laptop
# expose the same surface.
SERVICES: dict[str, tuple[str, int]] = {
    "cb-gateway": ("cb_gateway.main:app", 8081),
    "cb-api": ("cb_api.main:app", 8000),
}


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw.isdigit() else default


def run_asgi(target: str, default_port: int) -> int:
    from granian import Granian
    from granian.constants import Interfaces, Loops

    Granian(
        target,
        address=os.environ.get("CB_BIND_HOST", "0.0.0.0"),
        port=_int_env("CB_BIND_PORT", default_port),
        interface=Interfaces.ASGI,
        # One worker per container: replicas are the scaling unit, and a second
        # worker would double the connection pools behind the same limits.
        workers=_int_env("CB_WORKERS", 1),
        # granian 2.x renamed the loop knobs; `loop` takes the implementation
        # name. Anything set here must exist in the installed version — the
        # constructor raises TypeError on an unknown keyword, at startup, in the
        # cluster.
        loop=Loops.auto,
        log_enabled=False,  # structlog owns stdout (cb_core.logging)
    ).serve()
    return 0


def run_worker() -> int:
    from arq import run_worker as arq_run_worker

    from cb_worker.main import WorkerSettings

    arq_run_worker(WorkerSettings)  # type: ignore[arg-type]
    return 0


def run_migrations() -> int:
    """Converge the schema, then exit. Used as an init container.

    Every service calls `ensure_schema` at startup anyway (cb_core.migrations),
    so this changes nothing about correctness — it means N replicas do not race
    the same DDL on a cold namespace, and a failed migration fails the rollout
    instead of crash-looping three deployments at once.
    """
    import asyncio

    from cb_core.migrations import ensure_schema
    from cb_core.settings import get_settings

    revision = asyncio.run(ensure_schema(get_settings()))
    print(f"schema at revision {revision}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    service = (argv[0] if argv else os.environ.get("CB_SERVICE", "")).strip()

    if service in SERVICES:
        target, port = SERVICES[service]
        return run_asgi(target, port)
    if service == "cb-worker":
        return run_worker()
    if service == "migrate":
        return run_migrations()

    known = ", ".join([*SERVICES, "cb-worker", "migrate"])
    print(f"unknown service {service!r}; set CB_SERVICE to one of: {known}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
