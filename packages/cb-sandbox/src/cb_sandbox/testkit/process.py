"""Start a sandbox server for a test run and wait until it actually answers.

Separate from the pytest plugin so a suite that manages its own processes (or
one that isn't pytest at all) can still use it, and so the plugin's fixtures
stay about wiring rather than about subprocess bookkeeping.

The server is started as a real subprocess on a real loopback port, not as an
in-process ASGI app. That is the point: the bot under test has to reach it
over HTTP with its own client, its own connection pooling and its own
long-poll timeouts, and every one of those has produced a real bug that an
in-process transport would have hidden.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import TracebackType
from typing import IO


def free_port() -> int:
    """Bind-then-release. The OS will not usually hand the port back out
    before the subprocess we are about to spawn binds it a few milliseconds
    later — a race in theory, never observed in practice, and the alternative
    (let the server pick and parse it back off its logs) couples this to one
    server's log format."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class SandboxProcess:
    """A running `cb_sandbox.app:app`, with its own port and its own database.

    Use as a context manager, or call `start()`/`stop()`. `base_url` is what a
    `SandboxClient` and the bot's own `TELEGRAM_API_BASE` both point at.
    """

    def __init__(
        self,
        *,
        db_path: str | Path | None = None,
        config_path: str | Path | None = None,
        port: int | None = None,
        log_path: str | Path | None = None,
        env: dict[str, str] | None = None,
        startup_timeout: float = 20.0,
    ) -> None:
        self.port = port or free_port()
        self.db_path = Path(db_path) if db_path is not None else None
        self.config_path = Path(config_path) if config_path is not None else None
        self.log_path = Path(log_path) if log_path is not None else None
        self.startup_timeout = startup_timeout
        self._extra_env = dict(env or {})
        self._process: subprocess.Popen[bytes] | None = None
        self._log_handle: IO[bytes] | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    # ------------------------------------------------------------ lifecycle

    def _command(self) -> list[str]:
        """Prefer granian (what the sandbox ships with), fall back to uvicorn.

        Both are looked for next to the *current interpreter* rather than on
        `PATH`: a suite running inside a virtualenv wants that virtualenv's
        server, and a `PATH` lookup in a shell that never activated it finds
        either the wrong one or nothing.
        """
        bindir = Path(sys.executable).parent
        granian = bindir / "granian"
        if granian.exists():
            return [
                str(granian),
                "--interface",
                "asgi",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
                # A Bot API client keeps its connection to the API base alive
                # and reuses it; a server's idle-connection handling can
                # reclaim it in the gap between two back-to-back requests at
                # startup, which the client surfaces as a disconnect rather
                # than retrying transparently. Against a loopback sandbox
                # handling a handful of requests, a fresh connection per call
                # is free; losing a real request to a stale one is not.
                "--no-http1-keep-alive",
                "cb_sandbox.app:app",
            ]
        return [
            sys.executable,
            "-m",
            "uvicorn",
            "--host",
            "127.0.0.1",
            "--port",
            str(self.port),
            "cb_sandbox.app:app",
        ]

    def _env(self) -> dict[str, str]:
        env = {
            **os.environ,
            # Unbuffered: the server's output otherwise sits in its own stdout
            # buffer until it exits, so a failing run's log tail would show
            # nothing at all from a process that is still running.
            "PYTHONUNBUFFERED": "1",
        }
        if self.db_path is not None:
            env["CB_SANDBOX_DB"] = str(self.db_path)
        if self.config_path is not None:
            env["CB_SANDBOX_CONFIG"] = str(self.config_path)
        env.update(self._extra_env)
        return env

    def start(self) -> SandboxProcess:
        if self.log_path is not None:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log_handle = self.log_path.open("wb")
        self._process = subprocess.Popen(
            self._command(),
            env=self._env(),
            stdout=self._log_handle or subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
        )
        self._wait_ready()
        return self

    def _wait_ready(self) -> None:
        """Poll `/healthz` until it answers, and fail with the server's own log
        rather than a bare timeout — a sandbox that died on a config error has
        already printed exactly why, and the one thing a test author must not
        have to do is go hunting for it."""
        import httpx

        deadline = time.monotonic() + self.startup_timeout
        last_error = "no attempt made"
        while time.monotonic() < deadline:
            if self._process is not None and self._process.poll() is not None:
                raise RuntimeError(
                    f"cb-sandbox exited with code {self._process.returncode} during startup."
                    f"{self._log_tail()}"
                )
            try:
                response = httpx.get(f"{self.base_url}/healthz", timeout=1.0)
                if response.status_code == 200:
                    return
                last_error = f"HTTP {response.status_code}"
            except Exception as exc:  # noqa: BLE001 - anything unreachable means "not up yet"
                last_error = str(exc)
            time.sleep(0.1)
        self.stop()
        raise RuntimeError(
            f"cb-sandbox never became ready at {self.base_url} within "
            f"{self.startup_timeout}s (last error: {last_error}).{self._log_tail()}"
        )

    def _log_tail(self, lines: int = 30) -> str:
        if self.log_path is None or not self.log_path.exists():
            return ""
        tail = self.log_path.read_text(errors="replace").splitlines()[-lines:]
        return "\n--- cb-sandbox log ---\n" + "\n".join(tail) if tail else ""

    def stop(self) -> None:
        if self._process is None:
            return
        self._process.terminate()
        try:
            self._process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait(timeout=5)
        self._process = None
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None

    def __enter__(self) -> SandboxProcess:
        return self.start()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()
