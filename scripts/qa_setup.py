# /// script
# requires-python = ">=3.13"
# dependencies = ["rich>=13.9", "httpx>=0.28", "asyncpg>=0.30"]
# ///
"""Stand the whole bot up locally and prove its HTTP API answers — one command.

    uv run scripts/qa_setup.py

That is the entire prerequisite list: `uv`, and Docker or podman. Nothing is
installed into your system Python; the inline metadata block above is PEP 723,
so `uv run` builds this script its own environment on first use and reuses it
afterwards.

Written for someone testing the API rather than writing it. `scripts/cb.py` is
the developer's task runner — one task per thing, each assuming you know which
thing you need. This is the other shape: it knows the order, it checks what is
already true before doing anything, and every step prints what it proved rather
than that it ran.

    uv run scripts/qa_setup.py            # doctor, database, schema, demo data,
                                          #   API, a token, and a smoke pass
    uv run scripts/qa_setup.py seed       # just re-seed the demo data
    uv run scripts/qa_setup.py token      # print a fresh access token
    uv run scripts/qa_setup.py smoke      # hit every endpoint and show the table
    uv run scripts/qa_setup.py env        # the shell exports for curl / pytest
    uv run scripts/qa_setup.py stop       # stop the API; add --all for the containers

Every step is idempotent. Running it twice is a supported thing to do, and the
second run is how you find out what changed.

## The one thing worth understanding before you read further

The Mini App API is not open. Every endpoint except the health checks needs a
bearer token, and the only way to *get* one is to present something Telegram
signed — `initData` from a Mini App, or the login widget's payload. A tester
with no bot, no Telegram account and no public URL therefore cannot log in at
all, which would make the API untestable locally.

So this script does what Telegram does: it puts a **local-only bot token** in
your `.env` and signs its own `initData` with it (`mint_init_data` below).
Nothing is faked or bypassed — the signature is real, `cb_api.miniapp` verifies
it the same way it verifies Telegram's, and a payload signed with the wrong
token is rejected here exactly as it would be in production. What the fake
token buys is that *you* hold the key, so you can mint a session for any user
id: an owner, a group admin, or a stranger who should be refused.

That is what makes the negative cases testable, and the negative cases are most
of this API's behaviour.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import asyncpg
import httpx
from rich.console import Console, Group
from rich.panel import Panel
from rich.rule import Rule
from rich.status import Status
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

ROOT = Path(__file__).resolve().parent.parent
API_DIR = ROOT / "packages" / "cb-api"
STATE_DIR = ROOT / ".qa"
PID_FILE = STATE_DIR / "api.pid"
LOG_FILE = STATE_DIR / "api.log"
SESSION_FILE = STATE_DIR / "session.json"

console = Console()

# --------------------------------------------------------------------- the demo

#: Not a Telegram token, and shaped like one on purpose so nothing downstream
#: has to special-case it. It is written to `.env` only when no token is
#: configured, and it is the key this script signs its own `initData` with.
DEV_BOT_TOKEN = "7000000000:AA-cookiebot-local-dev-only-not-a-real-token"

DEFAULT_DSN = "postgresql://cookiebot:cookiebot@localhost:5432/cookiebot"
API_URL = os.environ.get("CB_QA_API_URL", "http://localhost:8000")

#: Fixed ids, so seeding twice updates rather than duplicates and `reset` knows
#: exactly what it may delete. Far from the ranges `qa/integration/factories.py`
#: allocates, so a seeded database and a test run never collide.
OWNER_ID = 900_000_001
ADMIN_ID = 900_000_002
STRANGER_ID = 900_000_003
#: Administers the *second* group and nothing else. Without a second admin
#: there is no way to try "an admin of one group asking about another", which
#: is the case the 404-not-403 rule exists for.
OTHER_ADMIN_ID = 900_000_004
MEMBER_BASE = 900_000_100
GROUP_BASE = -1_002_000_000_000

#: What the seeded groups are called, in the order they are created.
GROUP_TITLES = ("QA Demo Chat", "QA Second Chat", "QA Quiet Chat")

#: Commands the seeded rollups pretend were used. Real command names, so the
#: analytics endpoints return something a reader recognises.
SEED_COMMANDS = ("dice", "meme", "config", "rules", "ship", "battle", "birthday")

SEED_MODELS = (("anthropic", "claude-opus-5"), ("openai", "gpt-4o-mini"))


@dataclass(frozen=True)
class Step:
    """One line of the final report: what was attempted, and what came back."""

    name: str
    ok: bool
    detail: str


def _ok(name: str, detail: str) -> Step:
    return Step(name, True, detail)


def _bad(name: str, detail: str) -> Step:
    return Step(name, False, detail)


def report(steps: Sequence[Step]) -> int:
    table = Table(box=None, pad_edge=False, show_header=False)
    table.add_column(width=3)
    table.add_column(style="bold", no_wrap=True)
    table.add_column(overflow="fold")
    for step in steps:
        mark = Text("✓", style="green") if step.ok else Text("✗", style="red")
        table.add_row(mark, step.name, step.detail)
    console.print(table)
    return 0 if all(step.ok for step in steps) else 1


# ------------------------------------------------------------------- the .env

#: Everything the API needs that a fresh clone does not have. Each value is
#: written only when the key is absent, so a `.env` you have edited is never
#: overwritten — the script's job is to make an unconfigured checkout work, not
#: to have opinions about a configured one.
REQUIRED_ENV: dict[str, str] = {
    "CB_ENV": "local",
    "CB_LOG_JSON": "false",
    "CB_BOT_TOKENS": json.dumps({"cookiebot": DEV_BOT_TOKEN}),
    "CB_OWNER_ID": str(OWNER_ID),
    "CB_PG_DSN": DEFAULT_DSN,
    "CB_REDIS_DSN": "redis://localhost:6379/0",
    "CB_TELEGRAM_INGEST": "polling",
    "CB_TRACES_ENABLED": "false",
}


def read_env() -> dict[str, str]:
    """`.env` as a mapping. Absent file is an empty one, not an error."""
    path = ROOT / ".env"
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


#: Values in `.env.example` that are prompts rather than settings. A key
#: holding one of these is *unset* as far as this script is concerned —
#: treating `123:REPLACE` as a configured bot token is how a fresh clone gets a
#: green setup run and a 400 from every token request.
_PLACEHOLDER_MARKERS = ("replace", "changeme", "your-", "xxxx")

#: Keys whose "unset" value is a real value elsewhere. `CB_OWNER_ID=0` is the
#: shipped default meaning "nobody owns this deployment", which is correct in
#: production and useless locally.
_ZERO_IS_UNSET = frozenset({"CB_OWNER_ID"})


def _needs_value(key: str, value: str) -> bool:
    if not value:
        return True
    lowered = value.lower()
    if any(marker in lowered for marker in _PLACEHOLDER_MARKERS):
        return True
    return key in _ZERO_IS_UNSET and value.strip() == "0"


def _set_in_env_file(path: Path, values: dict[str, str]) -> None:
    """Rewrite the keys that already have a line; append the rest.

    In place rather than appended-with-duplicates: both `python-dotenv` and this
    script's own reader take the last assignment, so duplicates would work — and
    a `.env` where `CB_BOT_TOKENS` appears twice with different values is a file
    nobody can read confidently afterwards.
    """
    lines = path.read_text().splitlines() if path.exists() else []
    remaining = dict(values)
    for index, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if key in remaining and not line.lstrip().startswith("#"):
            lines[index] = f"{key}={remaining.pop(key)}"
    if remaining:
        lines.append(f"\n# --- added by scripts/qa_setup.py on {date.today().isoformat()} ---")
        lines.extend(f"{key}={value}" for key, value in remaining.items())
    path.write_text("\n".join(lines) + "\n")


def ensure_env() -> Step:
    """Create `.env` from the example if missing, then fill in what is unset.

    Only what is unset: a `.env` you have edited is never overwritten. The
    script's job is to make an unconfigured checkout work, not to have opinions
    about a configured one.
    """
    path = ROOT / ".env"
    created = False
    if not path.exists():
        example = ROOT / ".env.example"
        path.write_text(example.read_text() if example.exists() else "")
        created = True

    existing = read_env()
    filled = {
        key: value
        for key, value in REQUIRED_ENV.items()
        if _needs_value(key, existing.get(key, ""))
    }
    if filled:
        _set_in_env_file(path, filled)
    for key, value in {**existing, **filled}.items():
        os.environ.setdefault(key, value)

    if created:
        return _ok(".env", f"created from .env.example; {len(filled)} local values filled in")
    if filled:
        return _ok(".env", f"filled in {', '.join(sorted(filled))}")
    return _ok(".env", "already configured — nothing changed")


def bot_token() -> str:
    """The first configured bot token — the key `initData` is signed with.

    First, not "the dev one": a checkout that already has a real token in
    `CB_BOT_TOKENS` should mint sessions against *that*, because that is what
    the running API will verify against.
    """
    raw = os.environ.get("CB_BOT_TOKENS") or read_env().get("CB_BOT_TOKENS") or ""
    try:
        tokens = json.loads(raw)
    except json.JSONDecodeError:
        return DEV_BOT_TOKEN
    if isinstance(tokens, dict) and tokens:
        return str(next(iter(tokens.values())))
    return DEV_BOT_TOKEN


def dsn() -> str:
    return os.environ.get("CB_PG_DSN") or read_env().get("CB_PG_DSN") or DEFAULT_DSN


# ------------------------------------------------------------------- preflight


def container_runtime() -> str | None:
    """`docker` or `podman`, whichever is installed — docker first when both
    are. Same rule as `scripts/cb.py`; nothing here may hardcode one."""
    for exe in ("docker", "podman"):
        if shutil.which(exe):
            return exe
    return None


def run(*cmd: str, cwd: Path | None = None, quiet: bool = False) -> subprocess.CompletedProcess:
    """Echo then run. Echoing matters: a step that fails must be reproducible
    by hand, and "it failed during setup" is not a bug report."""
    if not quiet:
        console.print(f"[dim]$ {' '.join(cmd)}[/dim]")
    return subprocess.run(cmd, cwd=cwd or ROOT, capture_output=True, text=True, check=False)


def doctor() -> list[Step]:
    steps: list[Step] = []

    version = sys.version_info
    steps.append(
        _ok("python", f"{version.major}.{version.minor}.{version.micro}")
        if version >= (3, 13)
        else _bad("python", f"{version.major}.{version.minor} — 3.13+ required")
    )

    uv = shutil.which("uv")
    steps.append(_ok("uv", uv) if uv else _bad("uv", "not on PATH — https://docs.astral.sh/uv/"))

    runtime = container_runtime()
    steps.append(
        _ok("containers", f"{runtime} found")
        if runtime
        else _bad("containers", "neither docker nor podman is on PATH")
    )

    steps.append(ensure_env())
    return steps


# ------------------------------------------------------------------- database


async def _connect(seconds: float = 2.0) -> asyncpg.Connection | None:
    """A connection, or `None` — never an exception.

    Named `seconds` rather than `timeout` so ruff's ASYNC109 stays on: that rule
    is about a `timeout` parameter that should have been an `asyncio.timeout`
    block, and here it is `wait_for`'s own argument.
    """
    try:
        return await asyncio.wait_for(asyncpg.connect(dsn()), timeout=seconds)
    except (TimeoutError, OSError, asyncpg.PostgresError):
        return None


def database_reachable() -> bool:
    async def check() -> bool:
        conn = await _connect()
        if conn is None:
            return False
        await conn.close()
        return True

    return asyncio.run(check())


def start_database(wait_seconds: int = 120) -> Step:
    """Start Citus and Valkey, then wait until Postgres actually answers.

    "The container is up" and "the database accepts connections" are between
    ten and forty seconds apart on a cold image pull, and every confusing
    first-run failure this script could produce lives in that gap.
    """
    if database_reachable():
        return _ok("database", "already accepting connections")

    runtime = container_runtime()
    if runtime is None:
        return _bad("database", "no container runtime; start Postgres yourself and set CB_PG_DSN")

    result = run(runtime, "compose", "up", "-d", "citus", "valkey")
    if result.returncode != 0:
        return _bad("database", (result.stderr or result.stdout).strip().splitlines()[-1:][0])

    deadline = time.monotonic() + wait_seconds
    with Status("waiting for Postgres to accept connections…", console=console):
        while time.monotonic() < deadline:
            if database_reachable():
                return _ok("database", f"citus + valkey up via {runtime}")
            time.sleep(2)
    return _bad(
        "database", f"containers started but nothing answered on {dsn()} in {wait_seconds}s"
    )


def migrate() -> Step:
    """`alembic upgrade head`, through the project environment.

    Not this script's environment: the migrations import the project's own
    packages, and running them anywhere else would be a second way to build the
    schema — the thing AGENTS.md §8 says not to introduce.
    """
    result = run("uv", "run", "alembic", "upgrade", "head", cwd=API_DIR)
    if result.returncode != 0:
        tail = (result.stderr or result.stdout).strip().splitlines()
        return _bad("schema", tail[-1] if tail else "alembic failed")
    applied = [line for line in result.stderr.splitlines() if "Running upgrade" in line]
    return _ok("schema", f"{len(applied)} migration(s) applied" if applied else "already at head")


# ------------------------------------------------------------------ demo data


async def _seed(groups: int, days: int) -> dict[str, Any]:
    """Write a small, believable deployment: a tenant with an owner, three
    groups with admins and members, and a month of rollups behind them.

    Everything is an upsert on the natural key, so re-seeding is a no-op for
    rows that already match — the same property the v1 importer has, and for
    the same reason: you should be able to run it again without thinking.
    """
    conn = await _connect(seconds=5.0)
    if conn is None:
        raise RuntimeError(f"no database at {dsn()}")
    try:
        await conn.execute(
            """
            INSERT INTO tenants (tenant_id, display_name, owner_ids, monthly_llm_budget_usd)
            VALUES ('cookiebot', 'Cookiebot', $1::bigint[], 50.00)
            ON CONFLICT (tenant_id) DO UPDATE
               SET owner_ids = EXCLUDED.owner_ids,
                   monthly_llm_budget_usd = EXCLUDED.monthly_llm_budget_usd
            """,
            [OWNER_ID],
        )

        people = [
            (OWNER_ID, "qa_owner", "QA Owner"),
            (ADMIN_ID, "qa_admin", "QA Admin"),
            (STRANGER_ID, "qa_stranger", "QA Stranger"),
            (OTHER_ADMIN_ID, "qa_other_admin", "QA Other Admin"),
        ]
        people += [(MEMBER_BASE + n, f"qa_member{n}", f"QA Member {n}") for n in range(1, 13)]
        await conn.executemany(
            """
            INSERT INTO users (user_id, username, first_name)
            VALUES ($1, $2, $3)
            ON CONFLICT (user_id) DO UPDATE SET username = EXCLUDED.username
            """,
            people,
        )

        group_ids: list[int] = []
        today = datetime.now(UTC).date()
        for index in range(groups):
            group_id = GROUP_BASE - index
            group_ids.append(group_id)
            title = GROUP_TITLES[index % len(GROUP_TITLES)]
            await conn.execute(
                """
                INSERT INTO groups (group_id, title, username, chat_type, skin)
                VALUES ($1, $2, $3, 'supergroup', 'cookiebot')
                ON CONFLICT (group_id) DO UPDATE SET title = EXCLUDED.title
                """,
                group_id,
                f"{title} {index + 1}" if index else title,
                f"qa_demo_{index + 1}",
            )
            await conn.execute(
                "INSERT INTO group_configs (group_id) VALUES ($1) ON CONFLICT DO NOTHING",
                group_id,
            )
            # Three kinds of caller, on purpose: `qa_admin` runs the first
            # group, `qa_other_admin` runs only the second, and `qa_stranger`
            # runs none. That is what makes both halves of the 404-not-403 rule
            # testable — a stranger asking, and an admin asking about somebody
            # else's group.
            admin_id = OTHER_ADMIN_ID if index == 1 else ADMIN_ID
            await conn.execute(
                """
                INSERT INTO group_admins (group_id, user_id, role)
                VALUES ($1, $2, 'creator') ON CONFLICT DO NOTHING
                """,
                group_id,
                admin_id,
            )
            members = [(group_id, MEMBER_BASE + n) for n in range(1, 5 + index * 3)]
            await conn.executemany(
                "INSERT INTO group_members (group_id, user_id) VALUES ($1, $2) "
                "ON CONFLICT DO NOTHING",
                members,
            )

            # A month of rollups. The last group is left deliberately quiet, so
            # "a group with no rows" is a case you can see rather than imagine.
            if index == groups - 1 and groups > 1:
                continue
            daily_rows = []
            command_rows = []
            llm_rows = []
            for back in range(days):
                day = today - timedelta(days=back)
                weight = (index + 1) * (5 + (back % 7))
                daily_rows.append(
                    (
                        group_id,
                        day,
                        weight * 11,
                        weight * 2,
                        max(0, 3 - back % 4),
                        back % 3,
                        back % 5,
                        max(0, back % 5 - 1),
                        weight // 2,
                        back % 7 == 0,
                        90 + (back % 11) * 15,
                        weight * 40,
                        round(weight * 0.012, 4),
                    )
                )
                for position, command in enumerate(SEED_COMMANDS):
                    if (back + position) % 3:
                        command_rows.append(
                            (
                                group_id,
                                day,
                                command,
                                max(1, weight // (position + 1)),
                                1 if (back + position) % 11 == 0 else 0,
                                40 + position * 25,
                            )
                        )
                for provider, model in SEED_MODELS:
                    llm_rows.append(
                        (
                            group_id,
                            day,
                            provider,
                            model,
                            weight // 3,
                            weight * 30,
                            weight * 12,
                            round(weight * 0.006, 4),
                            1 if back % 9 == 0 else 0,
                            0,
                        )
                    )
            await conn.executemany(
                """
                INSERT INTO group_daily_stats
                    (group_id, day, messages, commands, joins, leaves, captcha_issued,
                     captcha_solved, active_users, errors, p95_latency_ms, llm_tokens,
                     llm_cost_usd)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
                ON CONFLICT (group_id, day) DO UPDATE SET messages = EXCLUDED.messages
                """,
                [(*row[:9], int(row[9]), *row[10:]) for row in daily_rows],
            )
            await conn.executemany(
                """
                INSERT INTO command_daily_stats
                    (group_id, day, command, invocations, errors, p95_latency_ms)
                VALUES ($1,$2,$3,$4,$5,$6)
                ON CONFLICT (group_id, day, command) DO UPDATE
                   SET invocations = EXCLUDED.invocations
                """,
                command_rows,
            )
            await conn.executemany(
                """
                INSERT INTO llm_daily_cost
                    (group_id, day, provider, model, calls, input_tokens, output_tokens,
                     cost_usd, refusals, errors)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                ON CONFLICT (group_id, day, provider, model) DO UPDATE
                   SET calls = EXCLUDED.calls
                """,
                llm_rows,
            )
        return {"groups": group_ids, "days": days, "people": len(people)}
    finally:
        await conn.close()


def seed(groups: int = 3, days: int = 30) -> tuple[Step, list[int]]:
    try:
        result = asyncio.run(_seed(groups, days))
    except Exception as exc:  # noqa: BLE001 - any failure here is one message to the reader
        return _bad("demo data", str(exc)), []
    ids = result["groups"]
    return (
        _ok(
            "demo data",
            f"{len(ids)} groups, {result['days']} days of rollups, {result['people']} people",
        ),
        ids,
    )


async def _reset() -> int:
    conn = await _connect(seconds=5.0)
    if conn is None:
        raise RuntimeError(f"no database at {dsn()}")
    try:
        ids = [GROUP_BASE - index for index in range(len(GROUP_TITLES) + 5)]
        # The rollup tables have no foreign key to `groups`, so the cascade does
        # not reach them — they are deleted by hand, first, on purpose.
        for table in ("group_daily_stats", "command_daily_stats", "llm_daily_cost"):
            await conn.execute(f"DELETE FROM {table} WHERE group_id = ANY($1::bigint[])", ids)
        deleted = await conn.execute("DELETE FROM groups WHERE group_id = ANY($1::bigint[])", ids)
        await conn.execute(
            "DELETE FROM users WHERE user_id = ANY($1::bigint[])",
            [
                OWNER_ID,
                ADMIN_ID,
                STRANGER_ID,
                OTHER_ADMIN_ID,
                *(MEMBER_BASE + n for n in range(1, 13)),
            ],
        )
        return int(deleted.rsplit(" ", 1)[-1])
    finally:
        await conn.close()


# ---------------------------------------------------------------- the API


def api_pid() -> int | None:
    """The PID of an API this script started, if it is still alive."""
    if not PID_FILE.exists():
        return None
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)
    except (ValueError, ProcessLookupError, PermissionError):
        PID_FILE.unlink(missing_ok=True)
        return None
    return pid


def api_healthy(timeout: float = 1.0) -> bool:
    try:
        return httpx.get(f"{API_URL}/healthz", timeout=timeout).status_code == 200
    except httpx.HTTPError:
        return False


def start_api(wait_seconds: int = 60) -> Step:
    """Start `cb-api` in the background, or notice that one is already there.

    Detached and logged to `.qa/api.log` rather than run in the foreground: a
    setup script that ends by blocking on a server is one a tester has to run
    in a second terminal to use, and the point of this file is that there is
    only one terminal.
    """
    if api_healthy():
        return _ok("api", f"already answering on {API_URL}")

    STATE_DIR.mkdir(exist_ok=True)
    log = LOG_FILE.open("a")
    log.write(f"\n--- started by qa_setup.py at {datetime.now(UTC).isoformat()} ---\n")
    log.flush()
    process = subprocess.Popen(
        [
            "uv",
            "run",
            "granian",
            "--interface",
            "asgi",
            "--host",
            "127.0.0.1",
            "--port",
            "8000",
            "cb_api.main:app",
        ],
        cwd=ROOT,
        stdout=log,
        stderr=subprocess.STDOUT,
        # Its own process group, so `stop` can end granian's workers as well as
        # its supervisor — killing the parent alone leaves the port bound.
        start_new_session=True,
        env={**os.environ, "CB_ENV": "local"},
    )
    PID_FILE.write_text(str(process.pid))

    deadline = time.monotonic() + wait_seconds
    with Status("waiting for cb-api…", console=console):
        while time.monotonic() < deadline:
            if api_healthy():
                return _ok("api", f"started on {API_URL} (logs: .qa/api.log)")
            if process.poll() is not None:
                tail = LOG_FILE.read_text().strip().splitlines()[-3:]
                return _bad("api", "exited: " + " / ".join(tail))
            time.sleep(1)
    return _bad("api", f"no answer on {API_URL} in {wait_seconds}s — see .qa/api.log")


def stop_api() -> Step:
    pid = api_pid()
    if pid is None:
        return _ok("api", "not running (or not started by this script)")
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError) as exc:
        return _bad("api", f"could not stop {pid}: {exc}")
    PID_FILE.unlink(missing_ok=True)
    return _ok("api", f"stopped {pid}")


# ------------------------------------------------------------------- tokens


def mint_init_data(user_id: int, *, username: str = "qa", token: str | None = None) -> str:
    """Telegram's `initData`, signed the way Telegram signs it.

    HMAC-SHA256 over the `key=value` pairs sorted by key and newline-joined,
    under a key derived as `HMAC_SHA256("WebAppData", bot_token)`. `hash` is the
    signature and is excluded from the string it signs.

    Written out here from Telegram's published algorithm rather than by
    importing `cb_api.miniapp`: a test fixture that calls the code under test to
    build its input can only ever agree with it.
    """
    fields = {
        "auth_date": str(int(time.time())),
        "query_id": "AAH-qa-setup",
        "user": json.dumps(
            {"id": user_id, "first_name": username, "username": username},
            separators=(",", ":"),
        ),
    }
    check = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret = hmac.new(b"WebAppData", (token or bot_token()).encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def fetch_token(user_id: int, *, username: str = "qa") -> dict[str, Any]:
    """Exchange freshly-signed `initData` for a real access token."""
    response = httpx.post(
        f"{API_URL}/oauth2/token",
        json={
            "grant_type": "urn:cookiebot:params:oauth:grant-type:telegram-miniapp",
            "init_data": mint_init_data(user_id, username=username),
        },
        timeout=10.0,
    )
    response.raise_for_status()
    return dict(response.json())


def save_session(tokens: dict[str, dict[str, Any]], group_ids: Sequence[int]) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    SESSION_FILE.write_text(
        json.dumps(
            {
                "api_url": API_URL,
                "minted_at": datetime.now(UTC).isoformat(),
                "group_ids": list(group_ids),
                "users": {
                    "owner": OWNER_ID,
                    "admin": ADMIN_ID,
                    "stranger": STRANGER_ID,
                    "other_admin": OTHER_ADMIN_ID,
                },
                "tokens": {role: body["access_token"] for role, body in tokens.items()},
            },
            indent=2,
        )
        + "\n"
    )


def load_session() -> dict[str, Any]:
    if not SESSION_FILE.exists():
        return {}
    try:
        return dict(json.loads(SESSION_FILE.read_text()))
    except json.JSONDecodeError:
        return {}


# -------------------------------------------------------------------- smoke


@dataclass(frozen=True)
class Probe:
    """One request, and the status it is *supposed* to answer with.

    The expectation is the test. A probe that says "GET /admin/overview as the
    group admin → 403" is asserting the authorisation model, and it fails loudly
    if that model ever quietly relaxes.

    This table and `qa/api/test_smoke.py` overlap on purpose and are not the
    same thing. This one is part of a *setup report*: it runs with no pytest, no
    collection and no fixtures, seconds after the API came up, and its job is to
    tell a person who has never seen this repository that the thing they just
    started works. The pytest suite is what CI and a contributor run, with
    fixtures, parametrisation and assertions on bodies rather than statuses.
    Deleting either one would cost something the other does not provide.
    """

    role: str
    method: str
    path: str
    expect: int
    why: str


def probes(group_id: int, other_group: int) -> list[Probe]:
    return [
        Probe("none", "GET", "/healthz", 200, "health is open, by design"),
        Probe("none", "GET", "/me", 401, "everything else needs a token"),
        Probe("admin", "GET", "/me", 200, "who am I, and which groups do I run"),
        Probe("admin", "GET", f"/groups/{group_id}/config", 200, "the /config menu, as HTTP"),
        Probe("admin", "GET", f"/groups/{group_id}/rules", 200, "null body is normal, not a 404"),
        Probe("admin", "GET", f"/groups/{group_id}/welcome", 200, ""),
        Probe("admin", "GET", f"/groups/{group_id}/audit", 200, "who changed what"),
        Probe("admin", "GET", f"/groups/{group_id}/analytics/summary", 200, ""),
        Probe("admin", "GET", f"/groups/{group_id}/analytics/daily", 200, ""),
        Probe("admin", "GET", f"/groups/{group_id}/analytics/commands", 200, ""),
        Probe("admin", "GET", f"/groups/{group_id}/analytics/llm", 200, ""),
        Probe(
            "admin",
            "GET",
            f"/groups/{group_id}/analytics/daily?start=2027-03-01&end=2027-01-01",
            400,
            "a reversed window is an error, not a clamp",
        ),
        Probe(
            "stranger",
            "GET",
            f"/groups/{group_id}/config",
            404,
            "404 not 403 — a stranger may not probe which chat ids exist",
        ),
        Probe(
            "admin",
            "GET",
            f"/groups/{other_group}/config",
            404,
            "an admin of one group is a stranger to another (qa_other_admin runs that one)",
        ),
        Probe("owner", "GET", f"/groups/{group_id}/config", 200, "a tenant owner sees every group"),
        Probe("owner", "GET", "/admin/overview", 200, "reach, totals and the LLM budget"),
        Probe("owner", "GET", "/admin/analytics/daily", 200, "every group, summed per day"),
        Probe("owner", "GET", "/admin/analytics/groups", 200, "the busiest groups"),
        Probe("owner", "GET", "/admin/analytics/commands", 200, "and how many groups use each"),
        Probe("owner", "GET", "/admin/analytics/llm", 200, "fleet-wide spend"),
        Probe("owner", "GET", "/admin/groups", 200, "the directory, keyset-paginated"),
        Probe("owner", "GET", "/admin/tenant", 200, "how this deployment is configured"),
        Probe(
            "admin",
            "GET",
            "/admin/overview",
            403,
            "403 here, not 404: /admin has no chat id to hide",
        ),
        Probe("stranger", "GET", "/admin/groups", 403, "a stranger runs nothing"),
    ]


def smoke(tokens: dict[str, dict[str, Any]], group_ids: Sequence[int]) -> tuple[Table, bool]:
    group_id = group_ids[0]
    other = group_ids[1] if len(group_ids) > 1 else group_ids[0] - 1

    table = Table(title="what the API answered", title_justify="left", header_style="bold")
    table.add_column("as", no_wrap=True)
    table.add_column("request", overflow="fold")
    table.add_column("want", justify="right")
    table.add_column("got", justify="right")
    table.add_column("ms", justify="right")
    table.add_column("what it proves", overflow="fold", style="dim")

    everything_passed = True
    with httpx.Client(base_url=API_URL, timeout=15.0) as client:
        for probe in probes(group_id, other):
            headers = {}
            if probe.role != "none":
                headers["Authorization"] = f"Bearer {tokens[probe.role]['access_token']}"
            started = time.perf_counter()
            try:
                response = client.request(probe.method, probe.path, headers=headers)
                status_code: int | str = response.status_code
            except httpx.HTTPError as exc:
                status_code = type(exc).__name__
            elapsed = (time.perf_counter() - started) * 1000
            passed = status_code == probe.expect
            everything_passed &= passed
            table.add_row(
                probe.role,
                f"{probe.method} {probe.path}",
                str(probe.expect),
                Text(str(status_code), style="green" if passed else "bold red"),
                f"{elapsed:.0f}",
                probe.why,
            )
    return table, everything_passed


# --------------------------------------------------------------- the commands


def exports(tokens: dict[str, dict[str, Any]], group_ids: Sequence[int]) -> str:
    lines = [f"export CB_QA_API={API_URL}", f"export CB_QA_GROUP={group_ids[0]}"]
    lines += [
        f"export CB_QA_{role.upper()}_TOKEN={body['access_token']}" for role, body in tokens.items()
    ]
    return "\n".join(lines)


def next_steps(tokens: dict[str, dict[str, Any]], group_ids: Sequence[int]) -> Panel:
    curl = 'curl -s "$CB_QA_API/admin/overview" -H "Authorization: Bearer $CB_QA_OWNER_TOKEN" | jq'
    body = Group(
        Text("Everything is up. Three things you can do now:\n", style="bold"),
        Text("1. Call the API by hand — paste these, then the curl below:"),
        Syntax(exports(tokens, group_ids), "bash", theme="ansi_dark", word_wrap=True),
        Syntax(curl, "bash", theme="ansi_dark", word_wrap=True),
        Text("\n2. Read the schema: ", end=""),
        Text(f"{API_URL}/docs", style="cyan underline"),
        Text(" — every endpoint, live, with its shapes.\n"),
        Text("3. Run the test suite against it, then add to it:", style=""),
        Syntax(
            "python scripts/cb.py api-test        # smoke + contract + integration\n"
            "python scripts/cb.py api-docs        # regenerate the reference page\n"
            "python scripts/cb.py check           # the whole gate",
            "bash",
            theme="ansi_dark",
        ),
        Text(
            "\nTokens and ids are in .qa/session.json. `uv run scripts/qa_setup.py stop`\n"
            "ends the API; add --all to stop the containers too.",
            style="dim",
        ),
    )
    return Panel(body, title="next", border_style="green", padding=(1, 2))


def command_all(args: argparse.Namespace) -> int:
    console.print(Rule("Cookiebot — local API setup"))
    steps = doctor()
    if any(not step.ok for step in steps):
        report(steps)
        console.print("[red]fix the above, then run this again[/red]")
        return 1

    steps.append(start_database())
    if not steps[-1].ok:
        return report(steps)

    steps.append(migrate())
    if not steps[-1].ok:
        return report(steps)

    seed_step, group_ids = seed(groups=args.groups, days=args.days)
    steps.append(seed_step)
    if not seed_step.ok:
        return report(steps)

    steps.append(start_api())
    if not steps[-1].ok:
        return report(steps)

    try:
        tokens = {
            "owner": fetch_token(OWNER_ID, username="qa_owner"),
            "admin": fetch_token(ADMIN_ID, username="qa_admin"),
            "stranger": fetch_token(STRANGER_ID, username="qa_stranger"),
        }
    except httpx.HTTPError as exc:
        steps.append(_bad("tokens", str(exc)))
        return report(steps)

    scopes = tokens["owner"]["scope"].split()
    steps.append(
        _ok(
            "tokens",
            f"3 sessions minted; the owner's carries {len(scopes)} scopes "
            f"({'admin:read granted' if 'admin:read' in scopes else 'no admin:read'})",
        )
    )
    save_session(tokens, group_ids)

    report(steps)
    console.print()
    table, passed = smoke(tokens, group_ids)
    console.print(table)
    console.print()
    console.print(next_steps(tokens, group_ids))
    return 0 if passed else 1


def command_doctor(_: argparse.Namespace) -> int:
    return report(doctor())


def command_seed(args: argparse.Namespace) -> int:
    ensure_env()
    step, ids = seed(groups=args.groups, days=args.days)
    code = report([step])
    if ids:
        console.print(f"[dim]group ids: {', '.join(str(i) for i in ids)}[/dim]")
    return code


def command_reset(_: argparse.Namespace) -> int:
    ensure_env()
    try:
        removed = asyncio.run(_reset())
    except Exception as exc:  # noqa: BLE001 - one message is the whole output
        return report([_bad("reset", str(exc))])
    return report([_ok("reset", f"{removed} seeded group(s) and their rows deleted")])


def command_token(args: argparse.Namespace) -> int:
    ensure_env()
    role_ids = {
        "owner": OWNER_ID,
        "admin": ADMIN_ID,
        "stranger": STRANGER_ID,
        "other-admin": OTHER_ADMIN_ID,
    }
    user_id = args.user_id or role_ids.get(args.role, OWNER_ID)
    try:
        body = fetch_token(user_id, username=args.role)
    except httpx.HTTPError as exc:
        return report([_bad("token", f"{exc} — is the API running? `qa_setup.py start`")])
    if args.raw:
        print(body["access_token"])
        return 0
    console.print(
        Panel(
            Group(
                Text(f"user {user_id} — scopes: {body['scope']}", style="bold"),
                Text(""),
                Text(body["access_token"], overflow="fold"),
            ),
            title=f"access token ({body['expires_in']}s)",
            border_style="cyan",
        )
    )
    return 0


def command_smoke(_: argparse.Namespace) -> int:
    ensure_env()
    session = load_session()
    group_ids = session.get("group_ids") or [GROUP_BASE, GROUP_BASE - 1]
    try:
        tokens = {
            role: fetch_token(user_id, username=role)
            for role, user_id in (
                ("owner", OWNER_ID),
                ("admin", ADMIN_ID),
                ("stranger", STRANGER_ID),
            )
        }
    except httpx.HTTPError as exc:
        return report([_bad("tokens", f"{exc} — is the API running?")])
    save_session(tokens, group_ids)
    table, passed = smoke(tokens, group_ids)
    console.print(table)
    return 0 if passed else 1


def command_env(_: argparse.Namespace) -> int:
    session = load_session()
    if not session.get("tokens"):
        console.print("[red]no session yet — run `uv run scripts/qa_setup.py` first[/red]")
        return 1
    print(
        "\n".join(
            [
                f"export CB_QA_API={session['api_url']}",
                f"export CB_QA_GROUP={session['group_ids'][0]}",
                *(
                    f"export CB_QA_{role.upper()}_TOKEN={token}"
                    for role, token in session["tokens"].items()
                ),
            ]
        )
    )
    return 0


def command_start(_: argparse.Namespace) -> int:
    ensure_env()
    return report([start_api()])


def command_stop(args: argparse.Namespace) -> int:
    steps = [stop_api()]
    if args.all:
        runtime = container_runtime()
        if runtime is None:
            steps.append(_bad("containers", "no container runtime on PATH"))
        else:
            result = run(runtime, "compose", "down")
            steps.append(
                _ok("containers", "stopped")
                if result.returncode == 0
                else _bad("containers", (result.stderr or result.stdout).strip()[-200:])
            )
    return report(steps)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qa_setup.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command")

    def add(name: str, handler: Any, help_text: str) -> argparse.ArgumentParser:
        sub = subparsers.add_parser(name, help=help_text)
        sub.set_defaults(handler=handler)
        return sub

    add("doctor", command_doctor, "check the prerequisites and the .env, change nothing else")
    seed_parser = add("seed", command_seed, "write the demo groups, people and rollups")
    seed_parser.add_argument("--groups", type=int, default=3)
    seed_parser.add_argument("--days", type=int, default=30)
    add("reset", command_reset, "delete everything `seed` wrote")
    token_parser = add("token", command_token, "mint an access token and print it")
    token_parser.add_argument(
        "role", nargs="?", default="owner", choices=["owner", "admin", "stranger", "other-admin"]
    )
    token_parser.add_argument(
        "--user-id", type=int, help="any Telegram id, for a session of your own"
    )
    token_parser.add_argument("--raw", action="store_true", help="print the token alone, for $()")
    add("smoke", command_smoke, "hit every endpoint and show what it answered")
    add("env", command_env, "print the shell exports for the last session")
    add("start", command_start, "start cb-api in the background")
    stop_parser = add("stop", command_stop, "stop cb-api")
    stop_parser.add_argument("--all", action="store_true", help="stop the containers too")

    parser.add_argument("--groups", type=int, default=3, help="how many demo groups (default 3)")
    parser.add_argument("--days", type=int, default=30, help="days of rollups (default 30)")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    handler = getattr(args, "handler", command_all)
    if shutil.which("uv") is None:
        console.print("[red]uv is not on PATH — https://docs.astral.sh/uv/[/red]")
        return 2
    return int(handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
