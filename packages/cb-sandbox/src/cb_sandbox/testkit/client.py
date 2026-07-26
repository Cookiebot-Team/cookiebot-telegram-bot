"""A thin client for the sandbox control plane, plus the bounded-poll helper
every end-to-end bot test needs.

Deliberately synchronous. Nothing here needs to overlap two things at once,
and a plain `httpx.Client` + `time.sleep` poll loop is one fewer thing to get
wrong than threading async fixtures through the right event-loop scope across
subprocess-backed, session-scoped fixtures.

One method per control-API route, no behaviour of its own: what a test asserts
should trace straight back to a documented `/api/...` endpoint, never to logic
hiding inside a test helper.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

import httpx


def _seg(value: str) -> str:
    """Percent-encode a scenario id for use as one path segment.

    Scenario ids are caller-chosen strings, and the obvious choice for a test
    is something derived from its own identity — which is exactly the kind of
    value that carries characters a URL path treats as structure. Encoding
    here keeps this client honest about ids it did not mint. A slash still
    cannot survive: a `%2F` in a path is decoded before routing, so an id
    containing one is unroutable no matter what this does — keep ids
    slash-free (the plugin's own generated ids are).
    """
    return quote(value, safe="")


class SandboxClient:
    """Every `/api/...` route, typed as loosely as the server returns it.

    Construct it with an `httpx.Client` whose `base_url` is the sandbox, or
    use `SandboxClient.connect(base_url)`.
    """

    def __init__(self, http: httpx.Client) -> None:
        self._http = http

    @classmethod
    def connect(cls, base_url: str, *, timeout: float = 10.0) -> SandboxClient:
        return cls(httpx.Client(base_url=base_url, timeout=timeout))

    def close(self) -> None:
        self._http.close()

    def _post(self, path: str, json: dict[str, Any] | None = None) -> Any:
        response = self._http.post(path, json=json or {})
        response.raise_for_status()
        return response.json()

    def _get(self, path: str) -> Any:
        response = self._http.get(path)
        response.raise_for_status()
        return response.json()

    def _patch(self, path: str, json: dict[str, Any]) -> Any:
        response = self._http.patch(path, json=json)
        response.raise_for_status()
        return response.json()

    # --------------------------------------------------------------- the kit

    def kit(self) -> dict[str, Any]:
        """`GET /api/kit` — identity, seeds, presets, commands, features.

        What a test reads to stop hardcoding the bot: `kit()["bot"]["id"]` is
        the bot's id in whatever config this server came up with, so a suite
        never has to repeat a number that lives in `sandbox.config.json`.
        """
        return self._get("/api/kit")

    def bot_id(self) -> int:
        return int(self.kit()["bot"]["id"])

    def features(self) -> list[dict[str, Any]]:
        """`GET /api/features` — one row per configured feature, carrying the
        scenarios that exercised it and how each ended. The rollup a
        validation pass reads; a feature with `scenario_count == 0` is the
        interesting row, and it exists nowhere else."""
        return self._get("/api/features")

    def seed(self, scenario: str = "empty") -> dict[str, Any]:
        """A *world* fixture — which users and chats exist. Not to be confused
        with the run scenarios below, which annotate what a stretch of
        activity was checking. The control API uses the word for both; only
        this one creates anything."""
        return self._post("/api/seed", {"scenario": scenario})

    # ------------------------------------------------------- run scenarios

    def create_scenario(
        self,
        *,
        scenario_id: str,
        name: str,
        description: str | None = None,
        source: str | None = None,
        feature: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        activate: bool = True,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"id": scenario_id, "name": name, "activate": activate}
        if description is not None:
            body["description"] = description
        if source is not None:
            body["source"] = source
        if feature is not None:
            body["feature"] = feature
        if tags is not None:
            body["tags"] = tags
        if metadata is not None:
            body["metadata"] = metadata
        return self._post("/api/scenarios", body)

    def add_note(self, scenario_id: str, text: str, level: str = "info") -> dict[str, Any]:
        return self._post(
            f"/api/scenarios/{_seg(scenario_id)}/notes", {"text": text, "level": level}
        )

    def patch_scenario(self, scenario_id: str, **fields: Any) -> dict[str, Any]:
        return self._patch(f"/api/scenarios/{_seg(scenario_id)}", fields)

    def end_scenario(self, scenario_id: str, status: str | None = None) -> dict[str, Any]:
        body = {} if status is None else {"status": status}
        return self._post(f"/api/scenarios/{_seg(scenario_id)}/end", body)

    def deactivate_scenario(self) -> dict[str, Any]:
        return self._post("/api/scenarios/deactivate")

    # -------------------------------------------------------- users, chats

    def create_user(
        self, first_name: str, username: str, language_code: str = "en"
    ) -> dict[str, Any]:
        return self._post(
            "/api/users",
            {"first_name": first_name, "username": username, "language_code": language_code},
        )

    def create_chat(self, title: str, chat_type: str = "supergroup") -> dict[str, Any]:
        return self._post("/api/chats", {"title": title, "type": chat_type})

    def open_dm(self, user_id: int) -> dict[str, Any]:
        """ "This user pressed Start" — `POST /api/users/{id}/dm`. The chat it
        creates has the user's *own id*, which is the only id a handler
        answering privately (`bot.send_message(user_id, ...)`) ever has. Until
        it exists the bot's private replies come back `403 Forbidden: bot can't
        initiate conversation with a user`, exactly as on real Telegram."""
        return self._post(f"/api/users/{user_id}/dm")

    def join(self, chat_id: int, user_id: int, by_user_id: int | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"user_id": user_id}
        if by_user_id is not None:
            body["by_user_id"] = by_user_id
        return self._post(f"/api/chats/{chat_id}/join", body)

    def leave(self, chat_id: int, user_id: int, by_user_id: int | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"user_id": user_id}
        if by_user_id is not None:
            body["by_user_id"] = by_user_id
        return self._post(f"/api/chats/{chat_id}/leave", body)

    def patch_member(
        self,
        chat_id: int,
        user_id: int,
        *,
        role: str | None = None,
        anonymous: bool | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if role is not None:
            body["role"] = role
        if anonymous is not None:
            body["anonymous"] = anonymous
        return self._post(f"/api/chats/{chat_id}/members/{user_id}", body)

    # ---------------------------------------------------- messages, buttons

    def send_message(
        self,
        chat_id: int,
        user_id: int,
        *,
        text: str | None = None,
        reply_to_message_id: int | None = None,
        media: str | None = None,
        media_caption: str | None = None,
        anonymous: bool = False,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"user_id": user_id, "anonymous": anonymous}
        if text is not None:
            body["text"] = text
        if reply_to_message_id is not None:
            body["reply_to_message_id"] = reply_to_message_id
        if media is not None:
            body["media"] = media
        if media_caption is not None:
            body["media_caption"] = media_caption
        return self._post(f"/api/chats/{chat_id}/messages", body)

    def press_callback(
        self, chat_id: int, user_id: int, message_id: int, data: str
    ) -> dict[str, Any]:
        return self._post(
            f"/api/chats/{chat_id}/callback",
            {"user_id": user_id, "message_id": message_id, "data": data},
        )

    def state(self) -> dict[str, Any]:
        return self._get("/api/state")


# ------------------------------------------------------------- assertions


def calls_to(state: dict[str, Any], method: str, since: int = 0) -> list[dict[str, Any]]:
    """Every `api_calls` entry for `method` recorded after index `since`.

    The strongest assertion surface a bot suite has: it shows what the bot
    actually asked Telegram to do (`deleteMessage`, `restrictChatMember`,
    `banChatMember`, `answerCallbackQuery`, ...), including the calls a chat
    transcript cannot show at all.

    `since` is an index into the call log, captured before the action — the
    idiom is `since = len(sandbox.state()["api_calls"])`, act, then assert on
    `calls_to(..., since)`, so a passing assertion means "this action caused
    it" rather than "something, once, did".
    """
    return [c for c in state["api_calls"][since:] if c["method"] == method]


def messages_in(state: dict[str, Any], chat_id: int, since: int = 0) -> list[dict[str, Any]]:
    """`GET /api/state` renders `messages` as a JSON object, whose keys are
    therefore always strings even though `chat_id` is an int everywhere else
    in this client — the one place that mismatch needs papering over."""
    return state.get("messages", {}).get(str(chat_id), [])[since:]


def describe_recent_calls(state: dict[str, Any], n: int = 10) -> str:
    """Rendered into every `wait_for` timeout — "the bot never answered X
    within Ns" is useless on its own; the tail of `api_calls` is what makes a
    failure here diagnosable without re-running under a debugger."""
    tail = state.get("api_calls", [])[-n:]
    rendered = ", ".join(f"{call['method']}({call['payload']})" for call in tail)
    return f"last {len(tail)} api_calls: [{rendered}]"


def wait_for[T](
    poll: Callable[[], T | None],
    *,
    timeout: float,
    interval: float = 0.1,
    description: str,
    on_timeout: Callable[[], str] | None = None,
) -> T:
    """Bounded-poll for the bot's reaction. `poll` returns a truthy match or
    `None`; the first truthy result wins, and nothing here ever blocks longer
    than `timeout`.

    Use this instead of sleeping a fixed duration. A `sleep(2)` that passes
    today is a test that fails on a slower machine and, worse, one that hides
    a regression the day the bot gets slower — it asserts nothing about when
    the answer arrived, only that the suite waited long enough.
    """
    deadline = time.monotonic() + timeout
    while True:
        result = poll()
        if result is not None:
            return result
        if time.monotonic() >= deadline:
            detail = f" {on_timeout()}" if on_timeout is not None else ""
            raise AssertionError(f"the bot never {description} within {timeout}s.{detail}")
        time.sleep(interval)
