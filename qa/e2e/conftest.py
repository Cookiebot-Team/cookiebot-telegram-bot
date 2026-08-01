"""Fixtures for the real end-to-end suite.

`CB_QA_SANDBOX=1` (`qa/sandbox_harness.py`) feeds updates straight into the
aiogram dispatcher in-process and only *mirrors* them into the sandbox store —
`tg_sandbox.control_api` is never called and `getUpdates` polling never runs.
This package closes that gap: `tg_sandbox.app:app` and `cb_gateway.main:app`
run as two real subprocesses, wired exactly as `docs/site/content/docs/sandbox.mdx` describes
(`CB_TELEGRAM_API_BASE` + `CB_TELEGRAM_INGEST=polling`), and every scenario
drives the gateway purely by calling the sandbox's `/api/...` control surface
over real HTTP — the same surface a human clicks through in the web UI.

Deliberately synchronous throughout (subprocess management, HTTP calls, the
poll loop). A session-scoped async fixture that starts a subprocess has no
event loop of its own to hand a session-scoped `httpx.AsyncClient` or asyncpg
pool without also pinning every test's loop scope to match — `pytest-asyncio`
either mismatches scopes or requires ini-level configuration this task's file
list does not extend to pyproject.toml beyond one marker. `subprocess.Popen`,
`httpx.Client` and `time.sleep`-based polling need none of that, and a
same-process test suite of this shape has nothing to gain from async: nothing
here ever needs to overlap two waits at once.

Opt-in only. `pytest_collection_modifyitems` below skips every test in this
package unless `CB_RUN_E2E=1` is set — `scripts/cb.py test-e2e` sets it, `cb.py
test` never does — so a plain `pytest` run collects these modules (cheap: no
subprocess, no network, see the hook) and skips every item in one pass rather
than paying for two extra processes and a shared database on the fast CI gate.

Known trap, already fixed here rather than rediscovered: the gateway's dedupe
middleware is Valkey-backed, keyed on `update_id`
(`cb_gateway/middlewares.py:DedupeMiddleware`), and `tg_sandbox`'s own
`update_id` counter restarts at 1 on every `/api/reset` or `/api/seed`
(`SandboxStore.reset`). Two things keep that from silently eating this suite's
first updates as replays: a Valkey database index of its own
(`CB_E2E_REDIS_DSN`, default index 14 — never index 0, never qa/'s 15),
flushed before the gateway ever starts, and seeding the sandbox to a known
state *before* the gateway's first request rather than after — see
`gateway_process`.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import httpx
import psycopg
import pytest
import redis
from tg_sandbox.config import load_config

from qa.e2e.client import SandboxClient, calls_to, describe_recent_calls, wait_for

ROOT = Path(__file__).resolve().parents[2]
#: What makes telegram-sandbox *this* bot's sandbox — see `scripts/gen_sandbox_config.py`.
SANDBOX_CONFIG = ROOT / "sandbox.config.json"

#: The bot the sandbox is configured to be, read from the same file the
#: subprocess is pointed at (below) rather than repeated here — so the id, the
#: token prefix the gateway is started with, and the account this suite joins
#: into every group cannot drift apart. Loaded from the explicit path, not by
#: discovery: discovery depends on the working directory, and this constant is
#: read at import time, before anything has established what that is.
BOT_ID = load_config(SANDBOX_CONFIG).bot.id
#: Installed next to the interpreter in this uv-managed venv, same binary
#: `scripts/cb.py`'s `gateway`/`sandbox` tasks invoke via `uv run granian`.
GRANIAN = str(Path(sys.executable).with_name("granian"))

#: Set by `scripts/cb.py test-e2e`; never by `cb.py test`/`cb.py test-all`.
_RUN_ENV_VAR = "CB_RUN_E2E"

_DEFAULT_PG_DSN = "postgresql://cookiebot:cookiebot@localhost:5432/cookiebot"
_DEFAULT_REDIS_DSN = "redis://localhost:6379/14"

#: Every scenario in this suite runs once per language in this tuple. `en` is
#: the baseline; `pt` is the majority of real traffic and, before this suite
#: existed, had never been checked end to end at all (docs/site/content/docs/e2e.mdx's own
#: scenario table only ever exercised `en`). `es` is a real v1 language too,
#: but the task this tuple was added for is "prove a `pt` group gets
#: Portuguese", not "triple the suite" — adding it later is one line here.
_LANGUAGES: tuple[str, ...] = ("en", "pt")
#: What `setlang.derive_join_language` actually stores in
#: `group_configs.language` for each — v1's raw three-way output, not the
#: canonical codes `cb_core.locales` resolves them to. A group created by this
#: path has to look identical in the database to one v1 created.
_STORED_LANGUAGE: dict[str, str] = {"en": "eng", "pt": "pt", "es": "es"}


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Opt-in gate for the whole package. Every test here carries
    `pytestmark = pytest.mark.e2e`; without `CB_RUN_E2E=1` every one of them
    is marked skipped *before* any fixture runs, so a plain `cb.py test` never
    spins up a subprocess, let alone two, on account of this directory.
    """
    if os.environ.get(_RUN_ENV_VAR) == "1":
        return
    skip_e2e = pytest.mark.skip(
        reason="opt-in only — run `python scripts/cb.py test-e2e` (see docs/site/content/docs/e2e.mdx)"
    )
    for item in items:
        if "e2e" in item.keywords:
            item.add_marker(skip_e2e)


def _free_port() -> int:
    """Bind-then-release — the same trick `qa/sandbox_harness.py` uses for its
    in-process fake. The OS will not usually hand the port back out before the
    subprocess we are about to spawn binds it a few milliseconds later."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _run_db_path() -> Path:
    """Where this run's sandbox state lands. Fixed, and wiped at the start of
    each session rather than at the end: the file has to outlive the run for
    anyone to read it, and a run whose file was deleted on exit is exactly the
    evidence people want when a test failed."""
    override = os.environ.get("CB_E2E_SANDBOX_DB")
    return Path(override) if override else ROOT / "sandbox-e2e.duckdb"


def _pg_dsn() -> str:
    return os.environ.get("CB_E2E_PG_DSN", os.environ.get("CB_PG_DSN", _DEFAULT_PG_DSN))


def _redis_dsn() -> str:
    return os.environ.get("CB_E2E_REDIS_DSN", _DEFAULT_REDIS_DSN)


@dataclass
class ProcessHandle:
    process: subprocess.Popen[bytes]
    base_url: str
    log_path: Path


def _spawn(args: list[str], env: dict[str, str], log_path: Path) -> subprocess.Popen[bytes]:
    log_file = log_path.open("wb")
    return subprocess.Popen(args, cwd=str(ROOT), env=env, stdout=log_file, stderr=subprocess.STDOUT)


def _log_tail(log_path: Path, n: int = 40) -> str:
    try:
        lines = log_path.read_text(errors="replace").splitlines()
    except OSError as exc:
        return f"(could not read {log_path}: {exc})"
    return "\n".join(lines[-n:])


def _wait_ready(
    process: subprocess.Popen[bytes], url: str, *, timeout: float, name: str, log_path: Path
) -> None:
    """Poll for readiness, never sleep-and-hope: return the instant `url`
    answers 200, fail immediately if the process has already died (so a crash
    on startup is reported as "it crashed", not as "it never became ready"),
    and fail loudly with the process's own log tail if it never comes up.
    """
    deadline = time.monotonic() + timeout
    last_status: int | None = None
    with httpx.Client(timeout=2.0) as client:
        while True:
            if process.poll() is not None:
                raise RuntimeError(
                    f"{name} exited (code {process.returncode}) before becoming ready.\n"
                    f"--- last lines of {log_path} ---\n{_log_tail(log_path)}"
                )
            try:
                response = client.get(url)
                last_status = response.status_code
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass  # still starting up; keep polling until the deadline
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"{name} never answered 200 at {url} within {timeout}s "
                    f"(last status: {last_status}).\n"
                    f"--- last lines of {log_path} ---\n{_log_tail(log_path)}"
                )
            time.sleep(0.2)


def _terminate(process: subprocess.Popen[bytes]) -> None:
    """Reliable teardown even when the test that requested the fixture failed:
    pytest still runs fixture finalizers, so this always executes."""
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=5)


@pytest.fixture(scope="session")
def _infra_ready() -> None:
    """The suite's one skip point. `docker-compose.yml`'s `citus`/`valkey`
    services (or an equivalent podman setup, `cb.py up`) must already be
    running — this only checks, exactly like `qa/conftest.py`'s
    `database`/`valkey` fixtures do for the acceptance suite. When they are
    reachable, nothing below is allowed to skip; a green run here means the
    suite actually ran.
    """
    pg_dsn = _pg_dsn()
    try:
        with psycopg.connect(pg_dsn, connect_timeout=3) as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
    except Exception as exc:  # noqa: BLE001 - any connection failure means "skip"
        pytest.skip(f"no Postgres at {pg_dsn}: {exc}")

    redis_dsn = _redis_dsn()
    client = redis.from_url(redis_dsn, socket_connect_timeout=3)
    try:
        if not client.ping():
            raise RuntimeError("PING returned a falsy response")
    except Exception as exc:  # noqa: BLE001 - any connection failure means "skip"
        pytest.skip(f"no Valkey at {redis_dsn}: {exc}")
    finally:
        client.close()


@pytest.fixture(scope="session")
def sandbox_process(
    _infra_ready: None, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[ProcessHandle]:
    """The real, unmodified `tg_sandbox.app:app`, on its own port and its own
    DuckDB file — never the developer's `sandbox.duckdb`.

    That file is deliberately *not* a `tmp_path` one: the run it records is
    worth reading afterwards. Every test tags its traffic with a scenario (see
    the `scenario` fixture below), so once the suite finishes, pointing a
    sandbox server at this file and opening the web UI shows every check this
    suite made, filterable down to one test — which is a far better answer to
    "what does the bot actually do" than a row of green dots. `docs/site/content/docs/e2e.mdx`
    has the two commands."""
    port = _free_port()
    db_path = _run_db_path()
    # Start from empty. Carrying the previous run's messages forward would make
    # every scenario dropdown in the UI a pile of stale, identically-named
    # entries, and `SandboxStore`'s id counters are what guarantee no update id
    # is ever reused — those live in `sandbox_counters`, which this deletes too,
    # but the gateway's Valkey dedupe database is flushed for the same run (see
    # `gateway_process`), so the two stay consistent with each other.
    for stale in (db_path, db_path.with_suffix(db_path.suffix + ".wal")):
        stale.unlink(missing_ok=True)
    log_path = tmp_path_factory.mktemp("telegram-sandbox-logs") / "sandbox.log"
    env = {
        **os.environ,
        # Unbuffered: structlog's output otherwise sits in the subprocess's
        # own stdout buffer until it exits, so a failure's log tail
        # (`_wait_ready`, `docs/site/content/docs/e2e.mdx`'s troubleshooting section) would show
        # nothing from a process still running.
        "PYTHONUNBUFFERED": "1",
        "CB_ENV": "e2e",
        "TG_SANDBOX_DB": str(db_path),
        # Explicit rather than discovered: the subprocess inherits this
        # process's working directory today, but a launcher that changes it
        # would silently give the sandbox the built-in defaults — a different
        # bot username, no features, no doomlist seed — and the symptom would
        # be a suite that fails for reasons nothing in it explains.
        "TG_SANDBOX_CONFIG": str(SANDBOX_CONFIG),
        "CB_TRACES_ENABLED": "false",
        "CB_LOG_JSON": "false",
        "CB_LOG_LEVEL": "WARNING",
    }
    process = _spawn(
        [
            GRANIAN,
            "--interface",
            "asgi",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            # aiogram's aiohttp session keeps its connection to the Bot API
            # base alive and reuses it; granian's own idle-connection handling
            # occasionally reclaims it in the gap between two back-to-back
            # requests at startup (`deleteWebhook` immediately followed by the
            # first `getUpdates` long-poll), which aiohttp surfaces as a
            # `ServerDisconnectedError` rather than transparently retrying.
            # Against a loopback, test-only sandbox handling a handful of
            # requests, paying for a fresh connection per call is free; losing
            # a real request to a stale one is not.
            "--no-http1-keep-alive",
            "tg_sandbox.app:app",
        ],
        env,
        log_path,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_ready(
            process, f"{base_url}/healthz", timeout=20, name="telegram-sandbox", log_path=log_path
        )
        yield ProcessHandle(process=process, base_url=base_url, log_path=log_path)
    finally:
        _terminate(process)


def _warm_up_database_pool(sandbox: SandboxClient, pg_conn: psycopg.Connection[Any]) -> None:
    """Pay the one-time Citus catalog-introspection cost here, in fixture
    setup, instead of in the first real test's `wait_for` budget.

    `cb_core.tenancy.TenantRegistry`'s own lookups select array-typed columns
    (`owner_ids bigint[]`, `disabled_commands text[]`), and asyncpg's first
    encounter with an array type on a fresh connection recursively introspects
    the whole Citus catalog — seconds long on an emulated container, the exact
    cost `qa/conftest.py`'s `database` fixture docstring names and works
    around with a 60s `pg_command_timeout` (set on the gateway subprocess
    above for the same reason). `TenantRegistry` caches by *method*, not just
    by tenant, with no TTL (`_local: dict[str, Tenant]`, keyed `tenant_id` for
    `by_id` and `"skin:{skin}"` for `by_skin`) — so `by_id` and `by_skin` are
    two independent cold starts even though they read the same columns. Both
    need priming or whichever scenario in this suite happens to be the first
    to call `/commands` (the only handler that reaches `by_skin`, via
    `_commands_available`) pays the cost that this fixture exists to avoid.
    `/privacy` only ever reaches `by_id` (via `context_for` ->
    `group_config.get_config`); `/commands` reaches both.
    """
    chat_id = int(sandbox.create_chat("e2e:warmup")["id"])
    # Self-heal a leaked row from an earlier, killed run — see `group_id`'s
    # identical comment; the same persistent-Postgres-vs-fresh-sandbox
    # mismatch applies to this throwaway group too.
    pg_conn.execute("DELETE FROM groups WHERE group_id = %s", (chat_id,))
    pg_conn.execute(
        "INSERT INTO groups (group_id, title, chat_type, skin) VALUES (%s, 'e2e warmup', 'supergroup', 'cookiebot')",
        (chat_id,),
    )
    # Captcha off, same reason as `group_id`: the warm-up user's self-join
    # would otherwise hit tg_sandbox's missing-reply-target gap and waste a
    # retry cycle on an exception this function does not care about.
    pg_conn.execute(
        "INSERT INTO group_configs (group_id, captcha_timeout_seconds) VALUES (%s, 0) "
        "ON CONFLICT (group_id) DO UPDATE SET captcha_timeout_seconds = 0",
        (chat_id,),
    )
    try:
        user_id = sandbox.create_user("Warmup", "e2e_warmup")["id"]
        sandbox.join(chat_id, user_id)
        for warmup_command in ("/privacy", "/commands"):
            since = len(sandbox.state()["api_calls"])
            sandbox.send_message(chat_id, user_id, text=warmup_command)

            def _got_a_reply(since: int = since) -> dict[str, Any] | None:
                # `since=since` binds this iteration's value now, not
                # whatever `since` is when the lambda finally runs (B023).
                return next(iter(calls_to(sandbox.state(), "sendMessage", since)), None)

            wait_for(
                _got_a_reply,
                # Generous, not lazy: aiogram's own polling loop
                # (`Dispatcher._polling`) treats *any* dropped first
                # connection — an occasional aiohttp/granian loopback hiccup
                # this suite has observed, unrelated to anything under test —
                # as a network fault and retries forever with growing
                # backoff, never giving up but occasionally taking the better
                # part of a minute to recover. Paying that once, here, keeps
                # every real scenario's own `wait_for` tight and meaningful
                # instead of every one of them needing to budget for a retry
                # storm that has nothing to do with what it is testing.
                timeout=90.0,
                description=f"answer the warm-up {warmup_command} round trip",
                on_timeout=lambda: describe_recent_calls(sandbox.state()),
            )
    finally:
        pg_conn.execute("DELETE FROM groups WHERE group_id = %s", (chat_id,))


@pytest.fixture(scope="session")
def gateway_process(
    sandbox_process: ProcessHandle,
    sandbox: SandboxClient,
    pg_conn: psycopg.Connection[Any],
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[ProcessHandle]:
    """The real, unmodified `cb_gateway.main:app`, polling `sandbox_process`
    instead of `api.telegram.org` — the pairing `docs/site/content/docs/sandbox.mdx` describes,
    started as a subprocess instead of by hand in a second terminal.
    """
    redis_dsn = _redis_dsn()
    flush_client = redis.from_url(redis_dsn)
    try:
        flush_client.flushdb()
    finally:
        flush_client.close()

    # Reset to a known-empty world *before* the gateway's own startup calls
    # `getMe()` (`cb_gateway.bots.BotRegistry.resolve_usernames`, run inside
    # `main.py`'s lifespan before the app serves a single request). That call
    # is what materialises the sandbox's bot user
    # (`tg_sandbox.telegram_api._ensure_bot_user`) — seeding *after* the
    # gateway is already up would wipe the very account this suite joins into
    # every group it creates (`/api/seed` clears every user, per
    # `SandboxStore.reset`). Seeding here, once, is also what keeps the
    # update_id counter's one reset harmless: nothing has asked for an
    # update_id yet, so there is nothing for the freshly flushed Valkey
    # dedupe set to collide with.
    httpx.post(
        f"{sandbox_process.base_url}/api/seed", json={"scenario": "empty"}, timeout=5
    ).raise_for_status()

    port = _free_port()
    metrics_port = _free_port()
    log_path = tmp_path_factory.mktemp("cb-gateway-logs") / "gateway.log"
    env = {
        **os.environ,
        "PYTHONUNBUFFERED": "1",
        "CB_ENV": "e2e",
        "CB_TELEGRAM_API_BASE": sandbox_process.base_url,
        "CB_TELEGRAM_INGEST": "polling",
        "CB_BOT_TOKENS": json.dumps({"cookiebot": "424242:SANDBOX"}),
        "CB_TRACES_ENABLED": "false",
        "CB_METRICS_PORT": str(metrics_port),
        "CB_REDIS_DSN": redis_dsn,
        "CB_PG_DSN": _pg_dsn(),
        # Same reason as qa/conftest.py's `database` fixture: the first
        # array-typed statement on a fresh connection triggers asyncpg's
        # recursive catalog introspection, which is seconds long against a
        # Citus catalog on an emulated container. At the 10s production
        # default it can expire mid-handler, and the symptom is not an error
        # in this suite — it is a handler that silently answered nothing.
        "CB_PG_COMMAND_TIMEOUT": "60",
        # The cost above is paid once *per connection*, not once per pool —
        # asyncpg caches type introspection on the connection object. The
        # production default (4) opens four connections at startup, so up to
        # four requests can each eat that one-time cost independently. This
        # suite's traffic is one message at a time; a single warm connection
        # is enough, and it means `_warm_up_database_pool` below only has to
        # win that race once instead of racing the pool's own connection count.
        "CB_PG_POOL_MIN": "1",
        # A short long-poll window, not Settings' 30s production default. This
        # is a local loopback poll against a sandbox that always answers in
        # milliseconds, so there is nothing to gain from holding a connection
        # open for 30s — and a lot to lose: aiogram's own polling loop treats
        # any dropped connection as a bug and retries with growing backoff, and
        # the sandbox now (correctly) answers a second concurrent long-poll
        # with `409 Conflict` (docs/site/content/docs/sandbox.mdx, "Bot API compatibility") until
        # the stale one's own timeout lapses server-side. At 30s that turns
        # one transient reconnect into a ~30s stall no per-test `wait_for`
        # budget should have to absorb; at a few seconds it clears in a few
        # seconds.
        "CB_TELEGRAM_POLLING_TIMEOUT": "3",
        "CB_WEBHOOK_SECRET": "e2e-unused-webhook-secret",
        "CB_LOG_JSON": "false",
        "CB_LOG_LEVEL": "WARNING",
        # util_config.feature:13 needs a real file_id configured to exercise
        # the anonymous-mode-tutorial-video branch — same setting and reason
        # as qa/conftest.py's identical line.
        "CB_ANONYMOUS_TUTORIAL_FILE_ID": "e2e-tutorial-file-id",
    }
    process = _spawn(
        [
            GRANIAN,
            "--interface",
            "asgi",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "cb_gateway.main:app",
        ],
        env,
        log_path,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_ready(process, f"{base_url}/readyz", timeout=45, name="cb-gateway", log_path=log_path)
        _warm_up_database_pool(sandbox, pg_conn)
        yield ProcessHandle(process=process, base_url=base_url, log_path=log_path)
    finally:
        _terminate(process)


@pytest.fixture(scope="session")
def sandbox(sandbox_process: ProcessHandle) -> Iterator[SandboxClient]:
    with httpx.Client(base_url=sandbox_process.base_url, timeout=10.0) as http:
        yield SandboxClient(http)


@pytest.fixture(scope="session")
def gateway(gateway_process: ProcessHandle) -> ProcessHandle:
    """Every test depends on this — usually transitively, through `group_id`
    — purely for its side effect: by the time it resolves, the real gateway is
    already up and polling the real sandbox."""
    return gateway_process


@pytest.fixture(scope="session")
def pg_conn(_infra_ready: None) -> Iterator[psycopg.Connection[Any]]:
    """A direct, synchronous connection to the *shared* Postgres this suite
    does not own — used only to insert/delete the `groups` row each test's
    `group_id` fixture needs (AGENTS.md §4: every one of `group_rules`,
    `group_members`, `captcha_challenges`, `group_configs`, `group_admins` has
    a `FOREIGN KEY (group_id) REFERENCES groups (group_id) ON DELETE CASCADE`,
    migration 0001), never to touch application tables directly.
    """
    conn = psycopg.connect(_pg_dsn(), autocommit=True)
    try:
        yield conn
    finally:
        conn.close()


def _language_code_for(language: str) -> str:
    """A Telegram client `language_code` that derives to `language`.

    `setlang.derive_join_language` ports v1's substring test verbatim
    (`'pt' in code` -> `"pt"`, `'es' in code` -> `"es"`, else `"eng"`), so
    these are real codes a real client sends, not the stored form.
    """
    return {"pt": "pt-BR", "es": "es-ES"}.get(language, "en-US")


def _wait_for_stored_language(
    pg_conn: psycopg.Connection[Any], chat_id: int, expected: str
) -> None:
    """Block until the gateway has written the language this group asked for.

    Also the assertion that the mechanism under test actually worked: the row
    is written by `setlang.on_bot_added_to_group` inside the running gateway,
    reacting to the join below, so seeing the expected value here means the
    production first-contact path ran — not that this fixture wrote something
    and hoped.
    """
    wait_for(
        lambda: (
            row[0]
            if (
                row := pg_conn.execute(
                    "SELECT language FROM group_configs WHERE group_id = %s", (chat_id,)
                ).fetchone()
            )
            and row[0] == expected
            else None
        ),
        timeout=20.0,
        description=f"let setlang derive group {chat_id}'s language as {expected!r}",
    )


def _make_group(
    sandbox: SandboxClient,
    pg_conn: psycopg.Connection[Any],
    title: str,
    captcha_timeout_seconds: int,
    language: str,
) -> Iterator[int]:
    """Shared body of `group_id` and `captcha_group_id`.

    The two disagree only on whether the captcha gate is open, and that has to
    be decided *before* the bot's join below: the join is the first thing that
    reads this group's config, and `cb_core/group_config.py` caches what it
    reads for 30s in-process — longer than any test here runs.

    The language is *not* pre-seeded the same way, because it is not this
    fixture's to write: the bot derives it from whoever adds it to the group.
    See the join below.
    """
    chat = sandbox.create_chat(title)
    chat_id = int(chat["id"])
    # `tg_sandbox`'s chat-id counter restarts at the same value every time a
    # process gets a fresh DuckDB file (every session of this suite), but
    # Postgres is the *shared*, persistent half of this fixture — so a group
    # id this session just minted can collide with a row an earlier, killed
    # (not merely failed — `finally` below already covers a failure) run of
    # this same suite never got to clean up. Deleting first is a no-op the
    # overwhelming majority of the time and a self-heal the one time it isn't;
    # relying on `ON CONFLICT` instead would silently keep that older run's
    # stale `group_configs`/`group_rules` rows under a title that looks like
    # this test's own.
    pg_conn.execute("DELETE FROM groups WHERE group_id = %s", (chat_id,))
    pg_conn.execute(
        "INSERT INTO groups (group_id, title, chat_type, skin) VALUES (%s, %s, 'supergroup', 'cookiebot')",
        (chat_id, title),
    )
    try:
        pg_conn.execute(
            "INSERT INTO group_configs (group_id, captcha_timeout_seconds) VALUES (%s, %s) "
            "ON CONFLICT (group_id) DO UPDATE SET "
            "captcha_timeout_seconds = EXCLUDED.captcha_timeout_seconds",
            (chat_id, captcha_timeout_seconds),
        )
        # Someone has to *add* the bot, and who adds it decides the group's
        # language: `setlang.on_bot_added_to_group` reads `language_code` off
        # whoever performed the add and writes `group_configs.language` from
        # it (v1's `COOKIEBOT.py:121-135`, verbatim). A bot cannot add itself
        # to a group on real Telegram, so a self-join here would be a shape
        # Telegram never produces — and one that derives every group to
        # `"eng"` from the bot's own record, silently overwriting any language
        # this fixture had pre-seeded.
        #
        # So the founder does it, with the client language a speaker of this
        # group's language would actually have. That makes the language
        # arrive by the production path instead of around it, and this suite
        # exercises `setlang`'s first-contact derivation for free rather than
        # working to defeat it.
        founder = sandbox.create_user(
            f"Founder{-chat_id}", f"founder{-chat_id}", language_code=_language_code_for(language)
        )["id"]
        sandbox.join(chat_id, BOT_ID, by_user_id=founder)
        _wait_for_stored_language(pg_conn, chat_id, _STORED_LANGUAGE[language])
        sandbox.patch_member(chat_id, BOT_ID, role="administrator")
        yield chat_id
    finally:
        # ON DELETE CASCADE (migration 0001) takes group_rules, group_members,
        # captcha_challenges, group_configs and group_admins with it — the
        # only cleanup a test needs, regardless of which of those it touched.
        pg_conn.execute("DELETE FROM groups WHERE group_id = %s", (chat_id,))


@pytest.fixture(params=_LANGUAGES)
def lang(request: pytest.FixtureRequest) -> str:
    """Which language this iteration's group is configured for.

    Parametrizing here rather than per-test is what runs the whole suite once
    per language for free: `group_id` and `captcha_group_id` below both depend
    on this fixture, so every test that uses either of them (every test in
    this package) is pulled into the same parametrization, and pytest folds
    the language into the test's own node id (`test_x[en]`, `test_x[pt]`) with
    no id plumbing of our own — `qa/e2e/conftest.py`'s `scenario` fixture
    builds its sandbox scenario id from exactly that node id, so the ids stay
    unique across languages the same way they already stay unique across test
    functions.

    The stored value is `pt`, not `pt-BR`: `group_configs.language` holds
    whatever a v1 import wrote verbatim (`"eng"`/`"pt"`/`"es"`,
    `cb_worker.importer`), and a v1 group's own `/config` writes the same
    literal `"pt"`. `cb_core.locales.resolve_language` normalises `"pt-BR"`,
    `"pt"` and v1's `"pt"` all down to the same canonical `pt`, so writing the
    literal store form is what an imported Portuguese group's row actually
    contains — not a Telegram `language_code`-shaped input the resolver merely
    also accepts.
    """
    return cast(str, request.param)


@pytest.fixture
def group_id(
    sandbox: SandboxClient,
    gateway: ProcessHandle,
    pg_conn: psycopg.Connection[Any],
    lang: str,
    request: pytest.FixtureRequest,
) -> Iterator[int]:
    """One fresh group per test, obvious in both worlds at once: a sandbox chat
    (the id every scenario drives through `/api/...`) backed by a `groups` row
    in the shared Postgres (the id every handler resolves config/rules/admins/
    captcha against). The title carries the test's own name — including the
    `[en]`/`[pt]` pytest already appends for `lang`'s two parameters — so a
    human opening the sandbox's own DuckDB file mid-run — or reading
    `docs/site/content/docs/e2e.mdx`'s troubleshooting section — can tell which test, in which
    language, owns which chat.

    The bot joins and is promoted to administrator here, once, because most of
    what this suite exercises (captcha, config, calladms' admin mention list,
    mediarestrict) first asks "is the bot even an admin here?" — the same
    reason `tg_sandbox.control_api._seed_default` seeds it for the web UI.
    Postgres is shared across the whole session (unlike the sandbox, which gets
    a fresh DuckDB file), so every test gets its own group id and cleans up its
    own row — never a shared/reseeded one, unlike the acceptance suite's single
    `GROUP_ID` (`qa/conftest.py`), because concurrent e2e runs against the same
    Postgres must not corrupt each other's fixtures.

    Captcha is off here, so a join reaches the join chain's *later* links
    (doomlist, welcome). Captcha is the chain's first link and, when on,
    nothing downstream of it runs at all — which is what `captcha_group_id` is
    for.
    """
    yield from _make_group(
        sandbox,
        pg_conn,
        f"e2e:{request.node.name}"[:128],
        captcha_timeout_seconds=0,
        language=lang,
    )


@pytest.fixture
def captcha_group_id(
    sandbox: SandboxClient,
    gateway: ProcessHandle,
    pg_conn: psycopg.Connection[Any],
    lang: str,
    request: pytest.FixtureRequest,
) -> Iterator[int]:
    """`group_id` with the captcha gate open, at v1's own default of 300s
    (`Configurations.py:111`). Only `test_captcha.py` wants this."""
    yield from _make_group(
        sandbox,
        pg_conn,
        f"e2e:{request.node.name}"[:128],
        captcha_timeout_seconds=300,
        language=lang,
    )


# --------------------------------------------------------------- scenarios
#
# The mechanics — one scenario per test, opened before any other function-scoped
# fixture so even the traffic `group_id` generates while building the world is
# attributed, and closed with the test's real outcome — now live in
# `tg_sandbox.testkit.plugin`, which ships with the sandbox and is loaded as a
# pytest plugin by installing it. What stays here is only what is specific to
# *this* suite: which feature each module checks, the language dimension, and
# the group id, none of which the sandbox could know.


@pytest.fixture(scope="session")
def sandbox_base_url(sandbox_process: ProcessHandle) -> str:
    """Point the test kit at the sandbox this suite already starts.

    Overriding this one fixture is the whole integration: the plugin would
    otherwise start a sandbox of its own, and this suite needs the gateway
    subprocess wired to the same one — started first, seeded before the
    gateway's first `getMe`, sharing the DuckDB file the terminal summary
    below tells the reader to open.

    Session-scoped to match the plugin's own declaration: `sandbox_kit` and
    `sandbox_bot_id` are session-scoped and read this, and a function-scoped
    override would turn the first use of either into a scope mismatch.
    """
    return sandbox_process.base_url


#: Which feature each e2e module exercises. The acceptance suite's modules are
#: already named after feature ids (`qa/test_core_rules.py` -> `core_rules`) and
#: match automatically; this package's are named after the *situation* they
#: drive, so the mapping has to be said out loud somewhere — here, next to the
#: tests, rather than buried in the generated config.
_MODULE_FEATURES: dict[str, str] = {
    "test_calladms": "util_calladms",
    "test_captcha": "core_groupguardian",
    "test_config_menu": "util_config",
    "test_join_chain": "core_welcome",
    "test_mediarestrict": "core_mediarestrict",
    "test_privacy_and_commands": "core_privacy",
    "test_rules": "core_rules",
    "test_stickerspam": "core_stickerspam",
}


@pytest.fixture
def sandbox_scenario_feature(request: pytest.FixtureRequest) -> str | None:
    """Which feature this test's scenario rolls up to.

    A `@pytest.mark.feature(...)` on the test wins, for the cases where one
    module covers two features; otherwise the module's own mapping applies, so
    a new test in an existing file is filed correctly without anyone
    remembering to mark it.
    """
    marker = request.node.get_closest_marker("feature")
    if marker is not None and marker.args:
        return str(marker.args[0])
    module = request.node.module.__name__.rsplit(".", 1)[-1] if request.node.module else ""
    return _MODULE_FEATURES.get(module)


@pytest.fixture
def sandbox_scenario_tags(lang: str) -> list[str]:
    """The language dimension, as a tag a person can filter the web UI on
    ("everything in pt") instead of having to open each scenario to find out
    which language it ran in.

    Depending on `lang` — a plain parametrized fixture with no side effects of
    its own — does not disturb the autouse ordering the scenario fixture
    relies on.
    """
    return [lang]


@pytest.fixture(autouse=True)
def _scenario_group_id(
    sandbox: SandboxClient, sandbox_scenario: str | None, request: pytest.FixtureRequest
) -> Iterator[None]:
    """Record which group a test drove, once it is knowable.

    It is minted by a fixture the scenario deliberately does not depend on —
    not every test uses a group — so it can only be read at teardown, out of
    the test's own resolved arguments. Without it, reading a failed scenario
    back in the web UI means guessing which of a run's dozens of groups was
    the one it was talking about.
    """
    yield
    if sandbox_scenario is None:
        return
    group_id = request.node.funcargs.get("group_id") or request.node.funcargs.get(
        "captcha_group_id"
    )
    if group_id is not None:
        sandbox.patch_scenario(sandbox_scenario, metadata={"group_id": group_id})


def pytest_terminal_summary(terminalreporter: Any) -> None:
    """Tell the reader where the run went. A suite whose whole point is that
    the run is *inspectable* should not make you go looking for the file."""
    if os.environ.get(_RUN_ENV_VAR) != "1":
        return
    db_path = _run_db_path()
    if not db_path.exists():
        return
    terminalreporter.write_sep("-", "sandbox recording")
    terminalreporter.write_line(f"  {db_path}")
    terminalreporter.write_line(
        f"  browse it:  TG_SANDBOX_DB={db_path} python scripts/cb.py sandbox"
    )
    terminalreporter.write_line("              python scripts/cb.py sandbox-web   # then :3001")
