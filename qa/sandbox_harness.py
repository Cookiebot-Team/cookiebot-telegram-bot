"""Plumbing for `CB_QA_SANDBOX=1` — running the acceptance suite through
`cb_sandbox` instead of `qa/mock_telegram.py`.

Default behaviour (the variable unset) never touches this module beyond the
top-level `import` in `qa/conftest.py`: every name that would actually import
`cb_sandbox` — which, on import, reconfigures logging via
`cb_sandbox.app`'s module-level `configure_logging(settings)` call — is
resolved lazily, inside a function body, so a default-mode run never pays for
or triggers any of it. Only `sandbox_enabled()` itself runs unconditionally,
and it does nothing but read an environment variable.

The plumbing has two halves:

1. `SandboxTelegram` — a class with the exact public surface every step file
   already imports `MockTelegram` for (`calls_to`, `admins`, `set_admins`,
   `fail`, `clear_failures`, `reset`, `base_url`, `start`/`stop`), so
   `qa/conftest.py`'s `telegram` fixture can hand out either one and no step
   file changes. It serves `cb_sandbox.app:app` — the *exact* app the real
   sandbox process runs, unmodified — over a real loopback TCP port, the
   same way `qa/mock_telegram.py` binds one, so `CB_TELEGRAM_API_BASE` keeps
   meaning what it always meant.

2. `mirror_inbound_update` — `qa/conftest.py:feed()` calls the dispatcher
   directly, skipping `cb_sandbox.control_api`'s `/api/...` surface entirely
   (the harness has always fed updates straight to `dispatcher.feed_update`,
   never through polling). That means the *inbound* half of a scenario — the
   user's own message, a join, a leave — has nothing that would otherwise
   put it in `cb_sandbox.state.store()`. This function does what
   `control_api.send_message`/`join_chat`/`leave_chat` would have done to the
   store, minus queuing a poll update the harness does not need.

Both halves lean on cb_sandbox's own store as the single source of truth
(`packages/cb-sandbox/src/cb_sandbox/state.py`, owned by another agent right
now — this module only imports and calls its public surface, never edits it).

One deliberate seam: cb_sandbox always requires the named chat/user to exist
before acting (`_require_chat` raises "chat not found" otherwise), because
its real usage always creates them first through `/api/...`. This harness
skips that step, so `_SandboxBridge` auto-creates a placeholder chat/user the
first time a Bot API call names one it has not seen — the same leniency
`qa/mock_telegram.py` has always had (it never checks a chat exists at all).
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from aiohttp import web

#: Opt-in only. Unset (the default) must reproduce today's suite exactly —
#: same fixture types, same speed, same pass count.
_SANDBOX_ENV_VAR = "CB_QA_SANDBOX"

#: Marks a chat/user this harness invented rather than one the human named,
#: so a later call that *does* carry a real name can safely upgrade it.
_PLACEHOLDER_CHAT_TITLE_PREFIX = "Sandbox chat "


def sandbox_enabled() -> bool:
    return os.environ.get(_SANDBOX_ENV_VAR) == "1"


# --------------------------------------------------------------- entity setup


def _ensure_chat(chat_id: int, *, title: str | None = None, chat_type: str | None = None) -> Any:
    """Get-or-create the `SandboxChat` a Bot API call or an inbound update
    names. Positive ids are Telegram's own convention for a private chat
    (chat_id == the user's id); everything else defaults to a group."""
    from cb_sandbox.state import SandboxChat
    from cb_sandbox.state import store as sandbox_store

    s = sandbox_store()
    chat = s.chats.get(chat_id)
    if chat is None:
        resolved_type = chat_type or ("private" if chat_id > 0 else "supergroup")
        chat = SandboxChat(
            id=chat_id,
            title=title or f"{_PLACEHOLDER_CHAT_TITLE_PREFIX}{chat_id}",
            type=resolved_type,  # type: ignore[arg-type]
        )
        s.chats[chat_id] = chat
    elif title is not None and chat.title.startswith(_PLACEHOLDER_CHAT_TITLE_PREFIX):
        chat.title = title
    return chat


def _ensure_user(
    user_id: int,
    *,
    first_name: str | None = None,
    username: str | None = None,
    is_bot: bool = False,
) -> Any:
    """Get-or-create the `SandboxUser` a Bot API call or an inbound update
    names. The placeholder name mirrors `qa/mock_telegram.py`'s own
    `_chat_member` synthesis (`Admin{id}`/`admin{id}`) for anything
    `set_admins` invents that a scenario never actually sent a message as."""
    from cb_sandbox.state import SandboxUser
    from cb_sandbox.state import store as sandbox_store

    s = sandbox_store()
    user = s.users.get(user_id)
    if user is None:
        user = SandboxUser(
            id=user_id,
            first_name=first_name or f"User{user_id}",
            username=username or f"user{user_id}",
            is_bot=is_bot,
        )
        s.users[user_id] = user
    elif first_name is not None and user.first_name == f"User{user_id}":
        user.first_name = first_name
        if username is not None:
            user.username = username
    return user


# ------------------------------------------------------------- inbound mirror


def _inbound_media_kind(message: dict[str, Any]) -> str | None:
    if "sticker" in message:
        return "sticker"
    if "photo" in message:
        return "photo"
    if "video" in message:
        return "video"
    if "animation" in message:
        return "animation"
    return None


def mirror_inbound_update(payload: dict[str, Any]) -> None:
    """Give the sandbox store a record of an update `qa/conftest.py:feed()`
    is about to hand straight to the dispatcher, bypassing
    `cb_sandbox.control_api` entirely. A no-op unless sandbox mode is on, so
    `feed()` can call this unconditionally without a caller-side branch."""
    if not sandbox_enabled():
        return
    message = payload.get("message")
    if message is not None:
        _mirror_message(message)
        return

    callback_query = payload.get("callback_query")
    if isinstance(callback_query, dict):
        _mirror_callback_query(callback_query)


def _mirror_callback_query(callback_query: dict[str, Any]) -> None:
    """Register the query id so the bot's answer is accepted.

    `cb_sandbox`'s `answerCallbackQuery` refuses an id it never issued —
    correctly, because that is what real Telegram does for a stale or invented
    id. But `queue_update` is what normally records one, and this harness
    never calls it: it hands the update straight to the dispatcher. Without
    this, every handler that answers a button press fails with "query is too
    old and response timeout expired or query id is invalid", which looks
    exactly like a handler bug and is not one.
    """
    from cb_sandbox.state import store as sandbox_store

    query_id = callback_query.get("id")
    if isinstance(query_id, str) and query_id:
        sandbox_store().register_callback_query(query_id)


def _store_service_message(message: dict[str, Any], chat_id: int, service: dict[str, Any]) -> None:
    """A join or a leave has to be a *stored* message, not just a membership
    change — exactly as `control_api.join_chat` stores one.

    Telegram models these as ordinary messages carrying `new_chat_members` /
    `left_chat_member` instead of text, and a handler that greets or challenges
    a newcomer answers with `message.reply(...)`, which sends
    `reply_to_message_id` pointing at the join. `cb_sandbox` looks that id up
    in the store, so without this every such reply comes back `400 Bad
    Request: message to reply not found` — which reads as "the welcome feature
    is broken" when the only broken thing is this bookkeeping.
    """
    from cb_sandbox.state import SandboxMessage
    from cb_sandbox.state import store as sandbox_store

    from_payload = message.get("from") or {}
    sandbox_store().add_message(
        SandboxMessage(
            message_id=message["message_id"],
            chat_id=chat_id,
            from_id=from_payload.get("id", service["user_id"]),
            text=None,
            date=float(message.get("date") or time.time()),
            service=service,
        )
    )


def _mirror_message(message: dict[str, Any]) -> None:
    from cb_sandbox.state import Membership, SandboxMessage
    from cb_sandbox.state import store as sandbox_store

    chat_payload = message["chat"]
    chat = _ensure_chat(
        chat_payload["id"], title=chat_payload.get("title"), chat_type=chat_payload.get("type")
    )

    from_payload = message.get("from")
    if from_payload is not None:
        _ensure_user(
            from_payload["id"],
            first_name=from_payload.get("first_name"),
            username=from_payload.get("username"),
            is_bot=bool(from_payload.get("is_bot", False)),
        )

    sender_chat_payload = message.get("sender_chat")
    sender_chat_id: int | None = None
    if sender_chat_payload is not None:
        sender_chat_id = sender_chat_payload["id"]
        _ensure_chat(
            sender_chat_id,
            title=sender_chat_payload.get("title"),
            chat_type=sender_chat_payload.get("type"),
        )

    s = sandbox_store()

    new_members = message.get("new_chat_members")
    if new_members:
        for member_payload in new_members:
            _ensure_user(
                member_payload["id"],
                first_name=member_payload.get("first_name"),
                username=member_payload.get("username"),
            )
            chat.members.setdefault(member_payload["id"], Membership(user_id=member_payload["id"]))
        _store_service_message(
            message,
            chat.id,
            {"kind": "join", "user_id": new_members[0]["id"], "by_user_id": None},
        )
        s.publish("member", {"chat_id": chat.id, "action": "join"})
        return

    left_member = message.get("left_chat_member")
    if left_member is not None:
        membership = chat.members.get(left_member["id"])
        if membership is not None:
            membership.role = "left"
        _store_service_message(
            message,
            chat.id,
            {"kind": "leave", "user_id": left_member["id"], "by_user_id": None},
        )
        s.publish("member", {"chat_id": chat.id, "action": "leave"})
        return

    text = message.get("text")
    media = _inbound_media_kind(message)
    if text is None and media is None:
        return  # nothing a human would see as a chat bubble (e.g. a bare service message)
    if from_payload is None:
        return  # malformed for this harness's purposes; nothing sane to attribute it to

    reply_to = message.get("reply_to_message")
    sandbox_message = SandboxMessage(
        message_id=message["message_id"],
        chat_id=chat.id,
        from_id=from_payload["id"],
        text=text,
        date=float(message.get("date") or time.time()),
        sender_chat_id=sender_chat_id,
        reply_to_message_id=reply_to.get("message_id") if reply_to else None,
        entities=message.get("entities", []),
        media=media,
        media_caption=message.get("caption"),
    )
    s.add_message(sandbox_message)
    s.publish("message", sandbox_message.as_telegram(s))


# ------------------------------------------------------------------ ASGI glue


class _SandboxBridge:
    """Sits in front of the real `cb_sandbox.app:app` (unmodified) to add two
    things that are test concerns, not sandbox-workbench concerns — see the
    module docstring: auto-vivifying the chat/user a call names, and failing
    a named method on demand (`SandboxTelegram.fail`, mirroring
    `qa/mock_telegram.py`'s own)."""

    def __init__(self, inner: Any, failures: dict[str, str]) -> None:
        self._inner = inner
        self.failures = failures

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self._inner(scope, receive, send)
            return

        from cb_sandbox.telegram_api import _extract_payload, _int
        from starlette.requests import Request

        body = bytearray()
        while True:
            chunk = await receive()
            body.extend(chunk.get("body", b""))
            if not chunk.get("more_body", False):
                break
        cached_body = bytes(body)

        async def cached_receive() -> dict[str, Any]:
            # Read twice, safely: both this class and the inner app read the
            # body exactly once each per request, so replaying the same
            # cached message to both is enough — no real "disconnect" state
            # needs modelling here.
            return {"type": "http.request", "body": cached_body, "more_body": False}

        request = Request(scope, receive=cached_receive)
        payload = await _extract_payload(request)

        chat_id = _int(payload, "chat_id")
        if chat_id is not None:
            _ensure_chat(chat_id)
        user_id = _int(payload, "user_id")
        if user_id is not None:
            _ensure_user(user_id)

        method = scope["path"].rsplit("/", 1)[-1]
        description = self.failures.get(method)
        if description is None:
            await self._inner(scope, cached_receive, send)
            return

        body_out = json.dumps({"ok": False, "error_code": 400, "description": description}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 400,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": body_out})


async def _serve_asgi(asgi_app: Any, request: web.Request) -> web.Response:
    """The other half of the bridge: translate one aiohttp request into one
    ASGI call and back. No streaming either direction — every response this
    harness needs (`/bot<token>/<method>`) is a single JSON body — so this
    stays a request/response adapter, not a general ASGI server."""
    body = await request.read()
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": request.method,
        "scheme": "http",
        "path": request.path,
        "raw_path": request.path.encode(),
        "query_string": request.query_string.encode(),
        "headers": [
            (key.lower().encode(), value.encode()) for key, value in request.headers.items()
        ],
        "client": (request.remote or "127.0.0.1", 0),
        "server": ("127.0.0.1", request.url.port or 0),
    }

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    started: dict[str, Any] = {}
    chunks: list[bytes] = []

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            started["status"] = message["status"]
            started["headers"] = message.get("headers", [])
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))

    await asgi_app(scope, receive, send)
    headers = {
        key.decode(): value.decode()
        for key, value in started.get("headers", [])
        if key.decode().lower() != "content-length"
    }
    return web.Response(status=started.get("status", 500), body=b"".join(chunks), headers=headers)


# --------------------------------------------------------------------- fixture


class SandboxTelegram:
    """`MockTelegram`'s drop-in for `CB_QA_SANDBOX=1`.

    Same public surface (`calls_to`, `admins`, `set_admins`, `fail`,
    `clear_failures`, `reset`, `base_url`, `start`/`stop`) so
    `qa/conftest.py`'s `telegram` fixture can hand out either one and no step
    file — every one of them types its fixture parameter `telegram:
    MockTelegram`, a hint Python never checks at runtime — needs to change.

    The one deliberate difference: the calls themselves live in
    `cb_sandbox.state.store()`, not on this object, so the sandbox's own web
    UI (or a human reading the store once `cb.py test` finishes) sees every
    call *every* scenario made, not just the current one's. `reset()` — the
    autouse `_clean` fixture calls it between scenarios so `calls_to`
    assertions stay scenario-scoped — only moves this object's own bookmark
    into that shared history; it never erases the store.
    """

    def __init__(self) -> None:
        self.failures: dict[str, str] = {}
        self.admins: dict[int, list[dict[str, Any]]] = {}
        self.port: int = 0
        self._call_offset: int = 0
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._bridge: _SandboxBridge | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def calls(self) -> list[tuple[str, dict[str, Any]]]:
        from cb_sandbox.state import store as sandbox_store

        return [
            (call["method"], call["payload"])
            for call in sandbox_store().api_calls[self._call_offset :]
        ]

    def calls_to(self, method: str) -> list[dict[str, Any]]:
        from cb_sandbox.state import store as sandbox_store

        return [
            call["payload"]
            for call in sandbox_store().api_calls[self._call_offset :]
            if call["method"] == method
        ]

    def fail(self, method: str, description: str = "Bad Request: test failure") -> None:
        self.failures[method] = description

    def clear_failures(self) -> None:
        self.failures.clear()

    def set_admins(
        self, chat_id: int, admins: list[tuple[int, str]], *, is_anonymous: bool = False
    ) -> None:
        """`admins` is (user_id, role) with role in {creator, administrator},
        same contract as `qa/mock_telegram.py`'s `set_admins` — including that
        the passed roster *replaces* the chat's admins, so anyone promoted by
        an earlier call and left out of this one is demoted back to member."""
        from cb_sandbox.state import Membership
        from cb_sandbox.state import store as sandbox_store
        from cb_sandbox.telegram_api import _chat_member_payload

        s = sandbox_store()
        chat = _ensure_chat(chat_id)
        keep = {user_id for user_id, _role in admins}
        for user_id, member in chat.members.items():
            if user_id not in keep and member.role in ("creator", "administrator"):
                member.role = "member"

        rendered: list[dict[str, Any]] = []
        for user_id, role in admins:
            _ensure_user(user_id, first_name=f"Admin{user_id}", username=f"admin{user_id}")
            member = chat.members.setdefault(user_id, Membership(user_id=user_id))
            member.role = role  # type: ignore[assignment]
            member.anonymous = is_anonymous
            rendered.append(_chat_member_payload(s, member))
        self.admins[chat_id] = rendered

    def reset(self) -> None:
        from cb_sandbox.state import store as sandbox_store

        self._call_offset = len(sandbox_store().api_calls)

    async def start(self) -> None:
        from cb_sandbox.app import app as sandbox_app

        self._bridge = _SandboxBridge(sandbox_app, self.failures)
        aio_app = web.Application()
        aio_app.router.add_route("*", "/{tail:.*}", self._handle)
        self._runner = web.AppRunner(aio_app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await self._site.start()
        # aiohttp assigns the port lazily; read it back off the bound socket,
        # same seam qa/mock_telegram.py uses.
        server = self._site._server  # noqa: SLF001
        self.port = server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self._site = None

    async def _handle(self, request: web.Request) -> web.Response:
        if self._bridge is None:
            raise RuntimeError("SandboxTelegram.start() was never called")
        return await _serve_asgi(self._bridge, request)
