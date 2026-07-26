"""One entry point for everything you run locally.

    python scripts/cb.py <task> [args...]
    python scripts/cb.py --list

There is no Makefile: one Python definition per task, used by developers and by
CI, so the two cannot drift. Tasks are plain functions returning an exit code.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORE = ROOT / "packages" / "cb-core"
API = ROOT / "packages" / "cb-api"

Task = Callable[[list[str]], int]
TASKS: dict[str, Task] = {}
HELP: dict[str, str] = {}


def task(name: str, help_text: str) -> Callable[[Task], Task]:
    def register(fn: Task) -> Task:
        TASKS[name] = fn
        HELP[name] = help_text
        return fn

    return register


def run(*cmd: str, cwd: Path | None = None, env: dict[str, str] | None = None) -> int:
    """Echo then run. Echoing matters: a failing task must be reproducible by hand."""
    printable = " ".join(cmd)
    where = f" (in {cwd.relative_to(ROOT)})" if cwd and cwd != ROOT else ""
    print(f"\033[36m$ {printable}\033[0m{where}", flush=True)
    merged = {**os.environ, **(env or {})}
    return subprocess.run(cmd, cwd=cwd or ROOT, env=merged, check=False).returncode


def container_runtime() -> str | None:
    """`docker` or `podman`, whichever is installed — docker first when both are.

    docker-compose.yml is plain OCI: `podman compose` reads the same file, so the
    only thing that varies is the executable name. Nothing else in the repo may
    hardcode `docker`.
    """
    for exe in ("docker", "podman"):
        if shutil.which(exe):
            return exe
    return None


def compose(*args: str) -> int:
    runtime = container_runtime()
    if runtime is None:
        print("neither docker nor podman is on PATH — install one of them")
        return 2
    return run(runtime, "compose", *args)


def chain(*steps: Callable[[], int]) -> int:
    """Run steps until one fails.

    Takes thunks, not results: passing already-evaluated calls would run every
    step before the first return value was ever inspected.
    """
    for step in steps:
        code = step()
        if code != 0:
            return code
    return 0


# --------------------------------------------------------------------- quality


@task("fmt", "ruff autofix + format")
def fmt(_: list[str]) -> int:
    return chain(
        lambda: run("uv", "run", "ruff", "check", "--fix", "."),
        lambda: run("uv", "run", "ruff", "format", "."),
    )


@task("types", "mypy on the packages + the compiled-module type audit")
def types(_: list[str]) -> int:
    """Two checks, because they cover different things.

    mypy sees what ruff's ANN rules cannot: a body that degrades to Any, a hint
    that disagrees with what the function returns. `hot_types.py` covers what
    neither sees — untyped *locals* in the Cython-compiled modules, where an
    annotation is lowered to a C type and its absence costs interpreter round
    trips.
    """
    return chain(
        lambda: run(
            "uv",
            "run",
            "mypy",
            "packages/cb-core/src",
            "packages/cb-api/src",
            "packages/cb-gateway/src",
            "packages/cb-worker/src",
        ),
        lambda: run("uv", "run", "python", "scripts/hot_types.py", "--check"),
    )


@task("lint", "ruff check + format --check (no writes)")
def lint(_: list[str]) -> int:
    return chain(
        lambda: run("uv", "run", "ruff", "check", "."),
        lambda: run("uv", "run", "ruff", "format", "--check", "."),
    )


# ----------------------------------------------------------------------- tests


@task("test", "unit + acceptance tests (no infrastructure)")
def test(extra: list[str]) -> int:
    return run("uv", "run", "pytest", "-q", "-m", "not integration", *extra)


@task("test-integration", "integration tests against a real Postgres/Citus")
def test_integration(extra: list[str]) -> int:
    return run("uv", "run", "pytest", "-q", "-m", "integration", "qa/integration", *extra)


@task("test-all", "every test, including integration")
def test_all(extra: list[str]) -> int:
    return run("uv", "run", "pytest", "-q", *extra)


@task("qa", "acceptance scenarios only")
def qa(extra: list[str]) -> int:
    return run("uv", "run", "pytest", "-q", "-m", "not integration", "qa", *extra)


@task("test-e2e", "real end-to-end: cb-gateway + cb-sandbox as subprocesses over HTTP")
def test_e2e(extra: list[str]) -> int:
    """Not part of `test`/`test-all`/`check` on purpose (see docs/E2E.md): it
    spins up two real processes and needs Postgres + Valkey already up
    (`cb.py up`), so it must never be what the fast CI gate pays for.
    `qa/e2e/conftest.py` skips cleanly when that infra is unreachable, and
    skips every test unless `CB_RUN_E2E=1` — set here, nowhere else.
    """
    return run("uv", "run", "pytest", "-q", "-m", "e2e", "qa/e2e", *extra, env={"CB_RUN_E2E": "1"})


@task("test-pyramid", "run each layer separately and report the shape")
def test_pyramid(_: list[str]) -> int:
    """Runs the layers bottom-up and stops at the first failure.

    Useful before a PR: it shows whether a change is covered at the layer it
    belongs to, instead of only at the top.
    """
    layers = [
        ("unit", ["uv", "run", "pytest", "-q", "packages"]),
        ("acceptance", ["uv", "run", "pytest", "-q", "-m", "not integration", "qa"]),
        ("integration", ["uv", "run", "pytest", "-q", "-m", "integration", "qa/integration"]),
    ]
    for name, cmd in layers:
        print(f"\n\033[1m── {name} ──\033[0m")
        code = run(*cmd)
        if code != 0:
            print(f"\033[31m{name} layer failed\033[0m")
            return code
    return 0


# ------------------------------------------------------------------ hot path


@task("cython", "compile the hot modules in place")
def cython(_: list[str]) -> int:
    return run("uv", "run", "python", "setup.py", "build_ext", "--inplace", cwd=CORE)


@task("bench", "benchmark the hot modules against the recorded baseline")
def bench(_: list[str]) -> int:
    return run("uv", "run", "python", "packages/cb-core/bench/bench_hot.py")


def _remove_inplace_extensions() -> int:
    """Delete the compiled `.so` files `cb.py cython` leaves in the source tree.

    Without this, `bench-baseline` silently measures the wrong build: the pure
    reinstall does not remove an in-place extension, so `import cb_core.cooldowns`
    still binds the compiled module, `COMPILED` stays true, and bench_hot.py — which
    only writes a baseline when *nothing* is compiled — skips writing one. The table
    it prints looks perfectly normal, so the stale baseline survives and every later
    speedup is measured against whatever machine conditions produced it.
    """
    removed = 0
    for artefact in (CORE / "src" / "cb_core").glob("*.so"):
        artefact.unlink()
        removed += 1
    print(f"\033[36m$ removed {removed} in-place extension(s)\033[0m", flush=True)
    return 0


@task("bench-baseline", "rebuild without cython and record the pure-python baseline")
def bench_baseline(_: list[str]) -> int:
    """Leaves the tree uncompiled — run `cb.py cython` afterwards to rebuild."""
    return chain(
        _remove_inplace_extensions,
        lambda: run("uv", "sync", "--reinstall-package", "cb-core", env={"CB_SKIP_CYTHON": "1"}),
        lambda: run("uv", "run", "python", "packages/cb-core/bench/bench_hot.py"),
    )


# --------------------------------------------------------------- infra + data


@task("up", "start citus, valkey, otel, prometheus, tempo, grafana (docker or podman)")
def up(extra: list[str]) -> int:
    return compose("up", "-d", *extra)


@task("observability", "prove the metrics/traces/logs stack actually works")
def observability(_: list[str]) -> int:
    """Push a real trace, log line and metric through the collector and query
    each store back out.

    `up` starting five containers proves nothing: a collector with the wrong
    exporter endpoint, a Loki silently rejecting timestamps and an idle
    application all render as the same empty dashboard. This tells them apart.
    """
    return run(sys.executable, str(ROOT / "scripts" / "verify_observability.py"))


@task("down", "stop infrastructure")
def down(_: list[str]) -> int:
    return compose("down")


@task("selfhosted", "also start the self-hosted Telegram Bot API server")
def selfhosted(_: list[str]) -> int:
    if not (os.environ.get("TELEGRAM_API_ID") and os.environ.get("TELEGRAM_API_HASH")):
        print("TELEGRAM_API_ID / TELEGRAM_API_HASH must be set (my.telegram.org)")
        return 2
    return compose("--profile", "selfhosted", "up", "-d")


@task("migrate", "apply alembic migrations")
def migrate(extra: list[str]) -> int:
    return run("uv", "run", "alembic", "upgrade", *(extra or ["head"]), cwd=API)


@task("import-mongo", "import v1 data from a MongoDB server or mongodump directory")
def import_mongo(extra: list[str]) -> int:
    """Idempotent: every write is an upsert on the natural key.

    That is what makes a cutover possible without a maintenance window — run it
    while v1 is still serving, then again at cutover to pick up the delta. Pass
    `--dry-run` to see the counts without writing.
    """
    return run("uv", "run", "python", "-m", "cb_worker.importer", *extra)


@task("migrate-check", "upgrade, downgrade to base, upgrade again")
def migrate_check(_: list[str]) -> int:
    return chain(
        lambda: run("uv", "run", "alembic", "upgrade", "head", cwd=API),
        lambda: run("uv", "run", "alembic", "downgrade", "base", cwd=API),
        lambda: run("uv", "run", "alembic", "upgrade", "head", cwd=API),
    )


# --------------------------------------------------------------- services


@task("gateway", "run the Telegram gateway")
def gateway(_: list[str]) -> int:
    return run(
        "uv",
        "run",
        "granian",
        "--interface",
        "asgi",
        "--host",
        "0.0.0.0",
        "--port",
        "8081",
        "cb_gateway.main:app",
    )


@task("api", "run the HTTP API")
def api(_: list[str]) -> int:
    return run(
        "uv",
        "run",
        "granian",
        "--interface",
        "asgi",
        "--host",
        "0.0.0.0",
        "--port",
        "8000",
        "cb_api.main:app",
    )


@task("sandbox-config", "regenerate sandbox.config.json from the spec and the parser")
def sandbox_config(_: list[str]) -> int:
    """What makes the bot-agnostic sandbox into *this* bot's sandbox.

    Re-run after adding a command alias or changing a feature's status in
    `scripts/spec.py` — otherwise the sandbox's command palette and its
    feature view describe the bot as it was, which is worse than describing
    nothing: a tester trusts them.
    """
    return run(sys.executable, str(ROOT / "scripts" / "gen_sandbox_config.py"))


@task("sandbox", "run the local Telegram sandbox API (:8083)")
def sandbox(_: list[str]) -> int:
    """The bot talks to this instead of Telegram; the web client drives it.

    `CB_SANDBOX_CONFIG` is set explicitly rather than left to discovery: the
    sandbox looks for `sandbox.config.json` from its working directory
    upwards, and a shell started in a subdirectory would silently get the
    built-in defaults — a different bot username, no features, no doomlist
    seed — with no error to explain why nothing matches.
    """
    return run(
        "uv",
        "run",
        "granian",
        "--interface",
        "asgi",
        "--host",
        "0.0.0.0",
        "--port",
        "8083",
        "cb_sandbox.app:app",
        env={"CB_SANDBOX_CONFIG": str(ROOT / "sandbox.config.json")},
    )


@task("sandbox-web", "run the sandbox web client (:3001)")
def sandbox_web(_: list[str]) -> int:
    """bun, not npm. `web/bun.lock` is the lockfile the repo commits, and a
    stray `npm install` writes a second one that resolves differently — the
    kind of drift that shows up as a build that works on one machine only."""
    web = ROOT / "web"
    if shutil.which("bun") is None:
        print("bun is not on PATH — install it: https://bun.sh (`brew install oven-sh/bun/bun`)")
        return 2
    if not (web / "node_modules").is_dir():
        print("installing web dependencies (first run only)")
        code = run("bun", "install", cwd=web)
        if code != 0:
            return code
    return run("bun", "run", "dev", cwd=web)


@task("sandbox-up", "print the wiring for the whole sandbox")
def sandbox_up(_: list[str]) -> int:
    """Prints instructions rather than daemonising three processes behind your back.

    The gateway must run with the sandbox as its Telegram API *and* with polling
    ingest, because the sandbox serves `getUpdates`. That pairing is the whole
    trick, and starting the gateway pointed at the real api.telegram.org by
    accident produces a UI where nothing ever answers.
    """
    lines = [
        "run these in three terminals:",
        "",
        "  1) python scripts/cb.py up          # postgres + valkey",
        "     python scripts/cb.py sandbox     # the fake Telegram   :8083",
        "        (reads sandbox.config.json — regenerate it with",
        "         python scripts/cb.py sandbox-config after a spec change)",
        "",
        "  2) CB_TELEGRAM_API_BASE=http://localhost:8083 \\",
        "     CB_TELEGRAM_INGEST=polling \\",
        '     CB_BOT_TOKENS=\'{"cookiebot": "424242:SANDBOX"}\' \\',
        "     python scripts/cb.py gateway     # the real bot, unmodified",
        "",
        "  3) python scripts/cb.py sandbox-web # the client          :3001  (bun)",
        "",
        "then open http://localhost:3001 and press a seed button.",
        "",
        "to assert the same wiring instead of clicking it: python scripts/cb.py test-e2e",
    ]
    print("\n".join(lines))
    return 0


@task("worker", "run the arq worker")
def worker(_: list[str]) -> int:
    return run("uv", "run", "arq", "cb_worker.main.WorkerSettings")


# ------------------------------------------------------------------- reporting


@task("status", "regenerate docs/MIGRATION-STATUS.md from the spec and a test run")
def status(extra: list[str]) -> int:
    return run("uv", "run", "python", "scripts/status.py", *extra)


@task("workflow", "validate .github/workflows/ci.yml with act (needs Docker)")
def workflow(extra: list[str]) -> int:
    """For editing the workflow itself, not for day-to-day development.

    Day to day, run the tasks directly — `check` runs the same commands the
    workflow runs, without a container. Reach for this only after changing
    `.github/workflows/ci.yml`, to confirm the YAML still does what you think.
    """
    if shutil.which("act") is None:
        print(
            "act is not installed.\n"
            "  macOS:  brew install act\n"
            "  other:  https://github.com/nektos/act\n"
            "It also needs a running Docker daemon.\n"
            "To check the code (not the workflow file), run: python scripts/cb.py check"
        )
        return 2
    runtime = container_runtime()
    if runtime is None:
        print("act needs a container runtime (docker, or podman with a Docker-compatible socket)")
        return 2
    probe = subprocess.run([runtime, "info"], capture_output=True, check=False)
    if probe.returncode != 0:
        print(f"act needs a running {runtime} daemon")
        return 2
    if runtime == "podman":
        # act speaks the Docker API; podman exposes it, but only when the socket
        # is published and pointed at explicitly.
        print(
            "note: act talks to the Docker API. With podman, run\n"
            "  podman machine start && export DOCKER_HOST=$(podman machine inspect "
            "--format '{{.ConnectionInfo.PodmanSocket.Path}}')\n"
            "before this task, or it will not find a daemon."
        )
    return run("act", "push", *(extra or ["--container-architecture", "linux/amd64"]))


@task("install", "sync the uv workspace")
def install(_: list[str]) -> int:
    return run("uv", "sync", "--all-packages")


@task("check", "the pre-push gate: lint, tests, bench, spec consistency")
def check(_: list[str]) -> int:
    return chain(
        lambda: lint([]),
        lambda: types([]),
        lambda: test([]),
        lambda: bench([]),
        lambda: run("uv", "run", "python", "scripts/status.py", "--check"),
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("task", nargs="?", help="task name")
    parser.add_argument("rest", nargs=argparse.REMAINDER, help="passed through to the task")
    parser.add_argument("--list", action="store_true", help="list tasks")
    args = parser.parse_args()

    if args.list or not args.task:
        width = max(len(name) for name in TASKS)
        print("tasks:")
        for name in sorted(TASKS):
            print(f"  {name:<{width}}  {HELP[name]}")
        return 0

    if args.task not in TASKS:
        print(f"unknown task {args.task!r}; try --list")
        return 2

    if shutil.which("uv") is None:
        print("uv is not on PATH — install it from https://docs.astral.sh/uv/")
        return 2

    return TASKS[args.task](args.rest)


if __name__ == "__main__":
    raise SystemExit(main())
