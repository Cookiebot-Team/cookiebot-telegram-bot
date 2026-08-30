"""One request-shaped helper, used by all three layers.

The suite is synchronous — `qa/conftest.py` owns a session event loop and hands
tests a `run()` that drives coroutines on it, so pytest-bdd's sync test bodies
and the async stack can share one loop. An `httpx.AsyncClient` therefore cannot
be awaited directly in a test body, and wrapping every call in `run(...)` at the
call site would put the plumbing in front of the assertion in every single test.

`Api` is that wrapper, once. `api.get("/me", token=tokens.admin)` reads like the
request it makes, and the same class drives both transports:

* **in-process** — `httpx.ASGITransport` over the real `cb_api.main.app`, no
  port, no server, no lifespan (`conftest.api`);
* **over the wire** — a plain `base_url` against whatever `qa_setup.py` started.

Both matter. In-process is what CI can run deterministically with nothing but a
database; over-the-wire is the only thing that proves a *deployment* — its
middleware, its process, its configuration — actually answers.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

import httpx

Run = Callable[[Coroutine[Any, Any, Any]], Any]


@dataclass(frozen=True)
class Tokens:
    """The three seeded callers, as bearer tokens.

    A named triple rather than a dict because every refusal test names one of
    them, and `tokens.stranger` fails a typo where `tokens["stragner"]` fails a
    lookup at run time in the middle of a test run.
    """

    owner: str
    admin: str
    stranger: str


class Api:
    """A synchronous view of an async HTTP client."""

    def __init__(self, client: httpx.AsyncClient, run: Run) -> None:
        self._client = client
        self._run = run

    def request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        json: Any | None = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        response: httpx.Response = self._run(
            self._client.request(method, path, headers=headers, json=json, params=params)
        )
        return response

    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("POST", path, **kwargs)

    def patch(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("PATCH", path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("PUT", path, **kwargs)


__all__ = ["Api", "Run", "Tokens"]
