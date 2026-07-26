"""Telegram's own Bot API surface, served locally.

Mounted at `/bot{token}/{method}` — the exact shape every Bot API client
targets (aiogram's `AiohttpSession.make_request`, python-telegram-bot's
`HTTPXRequest`, telegraf's fetch), so pointing a bot's API base here and
telling it to long-poll makes it drive this server exactly as it would drive
Telegram. Nothing in the bot is aware of the difference.

Every wire shape a real client sends is accepted. aiogram POSTs multipart form
data (`build_form_data`), never JSON: every value that isn't already a string
gets `json.dumps`-ed first, so an int arrives as `"123"`, a bool as
`"true"`/`"false"`, and a dict/list (`reply_markup`, `permissions`, `entities`)
as a JSON-encoded string. This module also accepts a plain JSON body and plain
query parameters — a test's `TestClient(...).post(..., json=...)` is a lot less
ceremony than building multipart forms by hand, and no real client needs the
difference rejected.

The failure mode this file exists to prevent: a Bot API response missing a
field the client's model marks required does not raise where you'd notice.
aiogram logs a validation warning and hands the handler `None` or an empty
collection, so an admin check quietly decides nobody is an admin, a media
handler quietly gets no file, and the test that was meant to prove a feature
works passes for the wrong reason. Every payload built here is validated
against aiogram's real models in `tests/test_telegram_api.py`, not just
spot-checked by eye — a fake Bot API that is only eyeballed against the docs
is a fake that eventually certifies a broken bot.

`SandboxStore` (`state.py`) is the only state. This module is a pure translator
between Telegram's wire shapes and it: every outbound message becomes a
`SandboxMessage` and a published `"message"` event (so the web client updates
live), every mutation to chat membership (`restrictChatMember`, `banChatMember`,
`promoteChatMember`, ...) actually changes the stored `Membership` (any
mute/captcha feature is only testable if that is real rather than cosmetic),
and every call is recorded with `store.record_api_call` — the tool's main
validation surface, shown to a human as "what did the bot actually do".

Two things earn their own section further down because getting them wrong is
easy and silent: `parse_mode`/entity parsing (real Telegram never stores raw
HTML/MarkdownV2 markup in `text` — it parses it into plain text + `entities`,
and a bot with a default `parse_mode` sends markup on nearly every message),
and the failure envelope (`{"ok": false, "error_code", "description"}`, plus
`parameters` where the real server sends it — see the package README's
compatibility table for the exact divergences from api.telegram.org this file
still has).
"""

from __future__ import annotations

import asyncio
import email
import json
import random
import re
import time
from collections.abc import Awaitable, Callable
from email.message import Message as EmailMessage
from typing import Any
from urllib.parse import parse_qsl

from cb_sandbox import config
from cb_sandbox.logging import get_logger
from cb_sandbox.state import (
    Membership,
    SandboxChat,
    SandboxMessage,
    SandboxStore,
    SandboxUser,
    store,
)
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

log = get_logger("cb.sandbox.telegram")

router = APIRouter()


def bot_id() -> int:
    """The sandbox's one bot identity — `getMe`'s answer and every outbound
    message's `from`, read from `cb_sandbox.config` rather than fixed here.

    Read through a call rather than bound to a module constant on purpose:
    the config is resolved lazily on first use, and a constant evaluated at
    import time would freeze whatever the environment happened to say before
    a launcher (or a test) had finished setting it up. One bot identity per
    process is still the rule — nothing in this tool needs two.
    """
    return config.bot().id


#: Where `_parse_multipart` stashes uploaded bytes on the payload dict:
#: `{field_name: (filename, data)}`. Prefixed so it can never be mistaken for
#: a real Bot API parameter, and never echoed back into the API-call log —
#: a megabyte of binary in the "what did the bot do" panel helps nobody.
_UPLOADS_KEY = "__uploads__"

#: What `getFile` answers for a `file_id` this sandbox has never stored — a
#: bot re-sending an id minted by production, or a seeded fixture. The download
#: still succeeds (failing closed here would break a handler for a reason that
#: has nothing to do with the handler), it just returns nothing meaningful.
_PLACEHOLDER_FILE_BYTES = b"cb-sandbox-placeholder-file"

#: `getUpdates` long-poll step. Small enough that a `timeout=25` poll returns
#: within a fraction of a second of an update landing, large enough not to
#: busy-loop the event loop.
_POLL_INTERVAL_S = 0.05


class TelegramApiError(Exception):
    """A Telegram-shaped failure: caught at the dispatch boundary and turned
    into `{"ok": false, "error_code": ..., "description": ...}`, never a 500.

    `parameters` mirrors the real `ResponseParameters` object
    (`retry_after`/`migrate_to_chat_id`) some errors carry so a client can
    handle them automatically. Nothing raised in this file needs it today —
    the sandbox does not simulate flood control or a group-to-supergroup
    migration (the README's divergence table says so) — but the wiring
    is real, not a stub: `tests/test_telegram_api.py` exercises it directly.
    """

    def __init__(
        self, status_code: int, description: str, *, parameters: dict[str, Any] | None = None
    ) -> None:
        super().__init__(description)
        self.status_code = status_code
        self.description = description
        self.parameters = parameters


# ------------------------------------------------------------- payload shape


def _parse_multipart(body: bytes, content_type_header: str) -> dict[str, Any]:
    """A MIME multipart body, by hand, with only the standard library.

    `Request.form()` would do this, but it hard-requires the optional
    `python-multipart` package even for the urlencoded case (its assertion
    fires before it looks at the content type at all) — a dependency this
    package does not carry, and adding one is not this file's call to make.
    `email.message_from_bytes` parses MIME multipart with nothing extra
    installed, which is all aiohttp's `FormData` (what aiogram actually
    sends) or httpx's multipart encoder produce.
    """
    header = f"Content-Type: {content_type_header}\r\nMIME-Version: 1.0\r\n\r\n".encode()
    message = email.message_from_bytes(header + body)
    payload: dict[str, Any] = {}
    if not message.is_multipart():
        return payload
    for part in message.get_payload():
        if not isinstance(part, EmailMessage):
            continue  # pragma: no cover - a multipart part is always a Message
        name = part.get_param("name", header="Content-Disposition")
        if not isinstance(name, str) or not name:
            continue
        filename = part.get_param("filename", header="Content-Disposition")
        if isinstance(filename, str) and filename:
            # An uploaded file. The bytes are kept, not discarded: a bot that
            # sends a generated image (a chart, a captcha, a resized thumbnail)
            # is exercising the feature most worth *looking at*, and a sandbox
            # that threw the upload away could only ever show a grey box where
            # the picture should be. Stashed under a private key so it cannot
            # collide with a real Bot API parameter name.
            raw = part.get_payload(decode=True)
            payload[name] = filename
            if isinstance(raw, bytes) and raw:
                payload.setdefault(_UPLOADS_KEY, {})[name] = (filename, raw)
            continue
        raw = part.get_payload(decode=True)
        data = raw if isinstance(raw, bytes) else b""
        charset = part.get_content_charset() or "utf-8"
        payload[name] = data.decode(charset, errors="replace")
    return payload


async def _extract_payload(request: Request) -> dict[str, Any]:
    """Every wire shape aiogram, Telegram's own docs, or a test might send.

    Query params are the lowest priority so a `GET .../getMe?foo=bar` style
    call still works; a JSON or form body overrides them field for field.
    """
    payload: dict[str, Any] = dict(request.query_params)
    content_type = request.headers.get("content-type", "")
    body = await request.body()
    if not body:
        return payload
    if "application/json" in content_type:
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            payload.update(data)
        return payload
    if "multipart/form-data" in content_type:
        payload.update(_parse_multipart(body, content_type))
        return payload
    if "application/x-www-form-urlencoded" in content_type:
        for key, value in parse_qsl(body.decode(), keep_blank_values=True):
            payload[key] = value
        return payload
    return payload


def _int(payload: dict[str, Any], key: str, default: int | None = None) -> int | None:
    value = payload.get(key)
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise TelegramApiError(400, f"Bad Request: {key} must be an integer") from exc


def _bool(payload: dict[str, Any], key: str, default: bool = False) -> bool:
    value = payload.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return bool(value)


def _str(payload: dict[str, Any], key: str, default: str | None = None) -> str | None:
    value = payload.get(key, default)
    if value is None:
        return default
    return str(value)


def _json_field(payload: dict[str, Any], key: str) -> Any:
    """`reply_markup`, `permissions`, `entities`: dicts/lists that arrive as a
    JSON-encoded string over multipart and as the real structure over JSON."""
    value = payload.get(key)
    if value is None or value == "":
        return None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise TelegramApiError(400, f"Bad Request: {key} is not valid JSON") from exc
    return value


def _require_int(payload: dict[str, Any], key: str) -> int:
    value = _int(payload, key)
    if value is None:
        raise TelegramApiError(400, f"Bad Request: {key} is required")
    return value


def _require_str(payload: dict[str, Any], key: str) -> str:
    value = _str(payload, key)
    if not value:
        raise TelegramApiError(400, f"Bad Request: {key} is required")
    return value


def _chat_ref(payload: dict[str, Any], key: str = "chat_id") -> int | str:
    """Real Telegram accepts either a numeric chat id or `@username` for any
    `chat_id`/`from_chat_id` parameter. `_require_chat` resolves either."""
    value = payload.get(key)
    if value is None or value == "":
        raise TelegramApiError(400, f"Bad Request: {key} is required")
    if isinstance(value, str) and value.startswith("@"):
        return value
    return _require_int(payload, key)


def _require_chat(s: SandboxStore, ref: int | str) -> SandboxChat:
    chat = s.chat_by_username(ref) if isinstance(ref, str) else s.chats.get(ref)
    if chat is not None:
        return chat
    # A positive id that names a known user is a DM the user has never opened.
    # Telegram refuses that with 403, not "chat not found": a bot may answer a
    # conversation but never start one. Handlers that DM someone
    # (`/config`'s "message me privately" fallback) depend on the difference,
    # and a tester seeing this error is seeing real Telegram behaviour, not a
    # sandbox limitation.
    if isinstance(ref, int) and ref in s.users:
        raise TelegramApiError(403, "Forbidden: bot can't initiate conversation with a user")
    raise TelegramApiError(400, "Bad Request: chat not found")


def _require_message(
    s: SandboxStore, chat_id: int, message_id: int, *, purpose: str = "edit"
) -> SandboxMessage:
    message = s.message(chat_id, message_id)
    if message is None or message.deleted:
        raise TelegramApiError(400, f"Bad Request: message to {purpose} not found")
    return message


def _ensure_bot_user(s: SandboxStore) -> None:
    """`as_telegram()` looks the sender up in `store.users`; without an entry
    for the bot itself, every message it sends would render with
    `is_bot: False`, which aiogram would happily parse and every admin/bot
    check downstream would happily get wrong."""
    identity = config.bot()
    if identity.id not in s.users:
        s.users[identity.id] = SandboxUser(
            id=identity.id,
            first_name=identity.first_name,
            username=identity.username,
            is_bot=True,
        )


def _resolve_reply_to(payload: dict[str, Any]) -> tuple[int | None, bool]:
    """The modern `reply_parameters` object and the legacy `reply_to_message_id`
    are two wire shapes for the same thing — aiogram's `Message.reply()` sends
    the former, a direct `bot.send_message(reply_to_message_id=...)` call the
    latter, and real Telegram still accepts either."""
    reply_parameters = _json_field(payload, "reply_parameters")
    if isinstance(reply_parameters, dict):
        message_id = reply_parameters.get("message_id")
        allow_without_reply = bool(reply_parameters.get("allow_sending_without_reply", False))
        return (int(message_id) if message_id is not None else None), allow_without_reply
    return _int(payload, "reply_to_message_id"), _bool(payload, "allow_sending_without_reply")


# ------------------------------------------------------- parse_mode / entities
#
# Real Telegram never stores the markup a caller sends: `sendMessage(text="<b>hi</b>",
# parse_mode="HTML")` arrives at every consumer of the resulting Message —
# aiogram, the web client, a second bot forwarding it — as `text="hi"` plus a
# `bold` entity spanning it. Before this section existed, the sandbox kept
# whatever string it was given verbatim, so a stored message showed literal
# `<b>...</b>` tags — invisible to a human as a bug because the web UI's
# `sanitizeHtml` renderer papered over it, but wrong for exactly what this
# tool exists to check: what would the real bot's output actually look like.
#
# Offsets and lengths are in **UTF-16 code units**, not Python's codepoint
# count — Telegram's own unit for `MessageEntity.offset`/`.length`. They only
# differ for a codepoint outside the Basic Multilingual Plane (most emoji),
# which is common enough in real chat text that getting this wrong would be a
# subtle, data-dependent bug rather than a theoretical one.


def _utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


_HTML_TAG_RE = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9-]*)((?:[^<>]*))>")
_HTML_ATTR_RE = re.compile(
    r'([a-zA-Z:-]+)\s*=\s*"([^"]*)"|([a-zA-Z:-]+)\s*=\s*\'([^\']*)\'|([a-zA-Z:-]+)'
)
_HTML_ENTITY_UNESCAPE_RE = re.compile(r"&(#x[0-9a-fA-F]+|#\d+|lt|gt|amp|quot|apos);")

#: Tags whose entity `type` is just the tag name looked up here — everything
#: with extra structure (`a`, `span`, `pre`, `blockquote`, `tg-emoji`) is
#: handled by `_html_entity` instead.
_HTML_SIMPLE_ENTITY_TYPES: dict[str, str] = {
    "b": "bold",
    "strong": "bold",
    "i": "italic",
    "em": "italic",
    "u": "underline",
    "ins": "underline",
    "s": "strikethrough",
    "strike": "strikethrough",
    "del": "strikethrough",
    "tg-spoiler": "spoiler",
    "code": "code",
}
_HTML_STRUCTURAL_TAGS = frozenset({"a", "span", "pre", "blockquote", "tg-emoji"})
_HTML_KNOWN_TAGS = frozenset(_HTML_SIMPLE_ENTITY_TYPES) | _HTML_STRUCTURAL_TAGS


def _unescape_html(chunk: str) -> str:
    def replace(match: re.Match[str]) -> str:
        token = match.group(1)
        named = {"lt": "<", "gt": ">", "amp": "&", "quot": '"', "apos": "'"}
        if token in named:
            return named[token]
        if token.startswith("#x"):
            return chr(int(token[2:], 16))
        return chr(int(token[1:]))

    return _HTML_ENTITY_UNESCAPE_RE.sub(replace, chunk)


def _parse_html_attrs(raw: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for match in _HTML_ATTR_RE.finditer(raw):
        if match.group(1) is not None:
            attrs[match.group(1).lower()] = match.group(2)
        elif match.group(3) is not None:
            attrs[match.group(3).lower()] = match.group(4)
        elif match.group(5) is not None:
            attrs[match.group(5).lower()] = ""
    return attrs


def _html_entity(
    tag: str, attrs: dict[str, str], offset: int, length: int
) -> dict[str, Any] | None:
    if length == 0:
        return None  # an empty tag pair produces no entity, matching real Telegram
    if tag == "a":
        href = attrs.get("href", "")
        if href.startswith("tg://user?id="):
            return {
                "type": "text_mention",
                "offset": offset,
                "length": length,
                "user": {"id": int(href.rsplit("=", 1)[-1]), "is_bot": False, "first_name": "User"},
            }
        return {"type": "text_link", "offset": offset, "length": length, "url": href}
    if tag == "span":
        if attrs.get("class") != "tg-spoiler":
            raise TelegramApiError(
                400, 'Bad Request: can\'t parse entities: Unsupported start tag "span"'
            )
        return {"type": "spoiler", "offset": offset, "length": length}
    if tag == "pre":
        # Real Telegram collapses `<pre><code class="language-x">...</code></pre>`
        # into one `pre` entity carrying `language`, rather than a separate
        # nested `code` entity — not reproduced here (nothing in this
        # codebase's locale strings uses it); see the README's divergence table.
        return {"type": "pre", "offset": offset, "length": length}
    if tag == "blockquote":
        kind = "expandable_blockquote" if "expandable" in attrs else "blockquote"
        return {"type": kind, "offset": offset, "length": length}
    if tag == "tg-emoji":
        return {
            "type": "custom_emoji",
            "offset": offset,
            "length": length,
            "custom_emoji_id": attrs.get("emoji-id", ""),
        }
    return {"type": _HTML_SIMPLE_ENTITY_TYPES[tag], "offset": offset, "length": length}


def _parse_html_entities(text: str) -> tuple[str, list[dict[str, Any]]]:
    """`parse_mode="HTML"`: turn inline markup into `(plain_text, entities)`
    the way the real server does, raising the same `can't parse entities`
    class of error on malformed markup real Telegram would reject — this is
    what makes a handler's own "the markup was rejected, retry in plain
    text" path exercisable in the sandbox at all; before this function
    existed, no markup was ever bad enough to fail here.
    """
    stack: list[tuple[str, dict[str, str], int]] = []
    parts: list[str] = []
    entities: list[dict[str, Any]] = []
    utf16_len = 0
    pos = 0
    for match in _HTML_TAG_RE.finditer(text):
        literal = _unescape_html(text[pos : match.start()])
        parts.append(literal)
        utf16_len += _utf16_length(literal)
        pos = match.end()
        closing = bool(match.group(1))
        tag = match.group(2).lower()
        if tag not in _HTML_KNOWN_TAGS:
            direction = "end" if closing else "start"
            raise TelegramApiError(
                400, f'Bad Request: can\'t parse entities: Unsupported {direction} tag "{tag}"'
            )
        if not closing:
            stack.append((tag, _parse_html_attrs(match.group(3)), utf16_len))
            continue
        if not stack or stack[-1][0] != tag:
            raise TelegramApiError(
                400, f'Bad Request: can\'t parse entities: Unexpected end tag "{tag}"'
            )
        open_tag, attrs, start = stack.pop()
        entity = _html_entity(open_tag, attrs, start, utf16_len - start)
        if entity is not None:
            entities.append(entity)
    parts.append(_unescape_html(text[pos:]))
    if stack:
        raise TelegramApiError(
            400,
            "Bad Request: can't parse entities: Can't find end tag corresponding to start "
            f'tag "{stack[-1][0]}"',
        )
    entities.sort(key=lambda entity: (entity["offset"], -entity["length"]))
    return "".join(parts), entities


#: MarkdownV2 delimiters that toggle open/close on repetition, keyed by the
#: entity `type` they produce. `_` (italic) is handled separately because its
#: two-character form `__` (underline) must be checked first.
_MD_TOGGLE_MARKERS: dict[str, str] = {"*": "bold", "~": "strikethrough"}


def _parse_markdown_v2_entities(text: str) -> tuple[str, list[dict[str, Any]]]:
    """`parse_mode="MarkdownV2"`: the same `(plain_text, entities)` contract as
    `_parse_html_entities`, for Telegram's other formatting language.

    Simplified relative to the real parser in one documented way: real
    Telegram requires every reserved character outside an entity to be
    backslash-escaped and rejects the message otherwise. Enforcing that here
    would make MarkdownV2 nearly unusable for a human typing sandbox test
    messages by hand (a stray `.` or `-` would 400), so this parser only
    escapes what it is asked to and does not demand escaping elsewhere — see
    the README's divergence table.
    """
    stack: list[tuple[str, int]] = []
    parts: list[str] = []
    entities: list[dict[str, Any]] = []
    utf16_len = 0
    i = 0
    n = len(text)

    def emit(chunk: str) -> None:
        nonlocal utf16_len
        if not chunk:
            return
        parts.append(chunk)
        utf16_len += _utf16_length(chunk)

    def toggle(kind: str) -> None:
        for index in range(len(stack) - 1, -1, -1):
            if stack[index][0] == kind:
                _, start = stack.pop(index)
                length = utf16_len - start
                if length > 0:
                    entities.append({"type": kind, "offset": start, "length": length})
                return
        stack.append((kind, utf16_len))

    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n:
            emit(text[i + 1])
            i += 2
            continue
        if text[i : i + 3] == "```":
            end = text.find("```", i + 3)
            if end == -1:
                raise TelegramApiError(
                    400,
                    f"Bad Request: can't parse entities: Can't find end of Pre entity at byte offset {i}",
                )
            body = text[i + 3 : end]
            newline = body.find("\n")
            language = None
            code_body = body
            if newline != -1 and body[:newline] and " " not in body[:newline]:
                language, code_body = body[:newline], body[newline + 1 :]
            start = utf16_len
            emit(code_body)
            if utf16_len > start:
                entity: dict[str, Any] = {
                    "type": "pre",
                    "offset": start,
                    "length": utf16_len - start,
                }
                if language:
                    entity["language"] = language
                entities.append(entity)
            i = end + 3
            continue
        if ch == "`":
            end = text.find("`", i + 1)
            if end == -1:
                raise TelegramApiError(
                    400,
                    f"Bad Request: can't parse entities: Can't find end of Code entity at byte offset {i}",
                )
            start = utf16_len
            emit(text[i + 1 : end])
            if utf16_len > start:
                entities.append({"type": "code", "offset": start, "length": utf16_len - start})
            i = end + 1
            continue
        if ch == "_" and text[i : i + 2] == "__":
            toggle("underline")
            i += 2
            continue
        if ch == "_":
            toggle("italic")
            i += 1
            continue
        if ch in _MD_TOGGLE_MARKERS:
            toggle(_MD_TOGGLE_MARKERS[ch])
            i += 1
            continue
        if text[i : i + 2] == "||":
            toggle("spoiler")
            i += 2
            continue
        if ch in "[!":
            is_emoji = ch == "!" and text[i : i + 2] == "!["
            if ch == "[" or is_emoji:
                label_start = i + (2 if is_emoji else 1)
                close_bracket = text.find("]", label_start)
                if close_bracket != -1 and text[close_bracket + 1 : close_bracket + 2] == "(":
                    url_end = text.find(")", close_bracket + 2)
                    if url_end != -1:
                        label = text[label_start:close_bracket]
                        url = text[close_bracket + 2 : url_end]
                        start = utf16_len
                        emit(label)
                        length = utf16_len - start
                        if length > 0:
                            entities.append(_md_link_entity(is_emoji, start, length, url))
                        i = url_end + 1
                        continue
            emit(ch)
            i += 1
            continue
        emit(ch)
        i += 1

    if stack:
        raise TelegramApiError(
            400, f"Bad Request: can't parse entities: Can't find end of {stack[-1][0]} entity"
        )
    entities.sort(key=lambda entity: (entity["offset"], -entity["length"]))
    return "".join(parts), entities


def _md_link_entity(is_emoji: bool, offset: int, length: int, url: str) -> dict[str, Any]:
    if is_emoji and url.startswith("tg://emoji?id="):
        return {
            "type": "custom_emoji",
            "offset": offset,
            "length": length,
            "custom_emoji_id": url.rsplit("=", 1)[-1],
        }
    if url.startswith("tg://user?id="):
        return {
            "type": "text_mention",
            "offset": offset,
            "length": length,
            "user": {"id": int(url.rsplit("=", 1)[-1]), "is_bot": False, "first_name": "User"},
        }
    return {"type": "text_link", "offset": offset, "length": length, "url": url}


def _apply_parse_mode(text: str, parse_mode: str) -> tuple[str, list[dict[str, Any]]]:
    normalized = parse_mode.strip().lower()
    if normalized == "html":
        return _parse_html_entities(text)
    if normalized in ("markdownv2", "markdown"):
        # Legacy "Markdown" is deprecated but still accepted by real Telegram;
        # reusing the MarkdownV2 engine is an approximation (no `__`/`~`/`||`
        # in the legacy dialect) rather than a second parser for a mode
        # nothing in this codebase sends — see the README's divergence table.
        return _parse_markdown_v2_entities(text)
    raise TelegramApiError(
        400, f"Bad Request: can't parse entities: Unsupported parse_mode {parse_mode!r}"
    )


def _formatted_text(
    payload: dict[str, Any], text_key: str, entities_key: str
) -> tuple[str | None, list[dict[str, Any]]]:
    """`text`+`parse_mode`, `text`+`entities`, or `caption`+the caption
    equivalents of either — real Telegram treats `parse_mode` and an explicit
    entities list for the same field as mutually exclusive."""
    raw = _str(payload, text_key)
    if raw is None:
        return None, []
    explicit_entities = _json_field(payload, entities_key)
    parse_mode = _str(payload, "parse_mode")
    if explicit_entities is not None and parse_mode:
        raise TelegramApiError(
            400,
            "Bad Request: can't parse entities: parse_mode and "
            f"{entities_key} are mutually exclusive",
        )
    if explicit_entities is not None:
        return raw, explicit_entities
    if parse_mode:
        return _apply_parse_mode(raw, parse_mode)
    return raw, []


# --------------------------------------------------------------- permissions

_PERMISSION_KEYS: tuple[str, ...] = (
    "can_send_messages",
    "can_send_audios",
    "can_send_documents",
    "can_send_photos",
    "can_send_videos",
    "can_send_video_notes",
    "can_send_voice_notes",
    "can_send_polls",
    "can_send_other_messages",
    "can_add_web_page_previews",
    "can_react_to_messages",
    "can_edit_tag",
    "can_change_info",
    "can_invite_users",
    "can_pin_messages",
    "can_manage_topics",
)
#: What `can_send_other_messages`/`can_add_web_page_previews` imply when
#: `use_independent_chat_permissions` is not set — `restrictChatMember`'s own
#: documented normalisation, not a sandbox invention.
_MEDIA_PERMISSION_KEYS: tuple[str, ...] = (
    "can_send_messages",
    "can_send_audios",
    "can_send_documents",
    "can_send_photos",
    "can_send_videos",
    "can_send_video_notes",
    "can_send_voice_notes",
)


def _normalize_permissions(permissions: dict[str, Any], independent: bool) -> dict[str, bool]:
    """`ChatPermissions` as real Telegram actually resolves it, not just as it
    arrived on the wire. Unless `use_independent_chat_permissions` is set,
    `can_send_other_messages`/`can_add_web_page_previews` each imply every
    "send media" permission, and `can_send_polls` implies `can_send_messages`.
    Skipping this step produces a `ChatMember` that looks internally
    consistent but reports the wrong thing a restricted user can actually do.
    """
    resolved = {key: bool(permissions.get(key, False)) for key in _PERMISSION_KEYS}
    if not independent:
        if resolved["can_send_other_messages"] or resolved["can_add_web_page_previews"]:
            for key in _MEDIA_PERMISSION_KEYS:
                resolved[key] = True
        if resolved["can_send_polls"]:
            resolved["can_send_messages"] = True
    return resolved


_RESTRICTION_FOREVER_MIN_SECONDS = 30
_RESTRICTION_FOREVER_MAX_DAYS = 366


def _normalize_until_date(until_date: int) -> float:
    """`restrictChatMember`/`banChatMember`'s own rule: a deadline under 30
    seconds or over 366 days from now is not a short restriction, it is
    forever — and "forever" is what every `ChatMember` payload represents as
    `until_date: 0`, not as some far-future timestamp."""
    if until_date <= 0:
        return 0.0
    delta = until_date - time.time()
    if delta < _RESTRICTION_FOREVER_MIN_SECONDS or delta > _RESTRICTION_FOREVER_MAX_DAYS * 86400:
        return 0.0
    return float(until_date)


#: Real Telegram's per-emoji dice ranges (`sendDice` docs): 🎲🎯🎳 roll 1-6,
#: 🏀⚽ roll 1-5, 🎰 rolls 1-64.
_DICE_RANGES: dict[str, int] = {"🎲": 6, "🎯": 6, "🎳": 6, "🏀": 5, "⚽": 5, "🎰": 64}


# ------------------------------------------------------------------- methods


async def _get_me(_s: SandboxStore, _payload: dict[str, Any]) -> dict[str, Any]:
    identity = config.bot()
    return {
        "id": identity.id,
        "is_bot": True,
        "first_name": identity.first_name,
        "username": identity.username,
        # Configured, not assumed: a client library carries these on its `User`
        # model and real code branches on them, so a bot whose BotFather
        # settings differ from the sandbox's would be tested against the wrong
        # answer to "do I even see this message".
        "can_join_groups": identity.can_join_groups,
        "can_read_all_group_messages": identity.can_read_all_group_messages,
        "supports_inline_queries": identity.supports_inline_queries,
    }


async def _get_updates(s: SandboxStore, payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Telegram's confirm-by-offset long poll: block up to `timeout` seconds,
    returning the moment an update is available and otherwise waiting out the
    full window. `SandboxStore.take_updates` already implements the "an update
    stays queued until a later offset asks past it" half of the contract; this
    is only the waiting half — plus the two things real Telegram also does
    here: reject a second concurrent poll with 409, and remember
    `allowed_updates` across calls that omit it.
    """
    offset = _int(payload, "offset")
    limit = _int(payload, "limit")
    if limit is not None and not (1 <= limit <= 100):
        raise TelegramApiError(400, "Bad Request: limit must be between 1 and 100")
    limit = limit or 100
    timeout = _int(payload, "timeout") or 0
    requested_allowed = _json_field(payload, "allowed_updates")
    if requested_allowed is not None:
        s.allowed_updates = requested_allowed
    if not s.begin_polling():
        raise TelegramApiError(
            409,
            "Conflict: terminated by other getUpdates request; "
            "make sure that only one bot instance is running",
        )
    try:
        elapsed = 0.0
        while True:
            updates = s.take_updates(offset=offset, limit=limit, allowed_updates=s.allowed_updates)
            if updates or elapsed >= timeout:
                return updates
            step = min(_POLL_INTERVAL_S, timeout - elapsed)
            await asyncio.sleep(step)
            elapsed += step
    finally:
        s.end_polling()


async def _set_webhook(_s: SandboxStore, _payload: dict[str, Any]) -> bool:
    # Polling is the sandbox's only transport; a webhook registration is
    # accepted and ignored rather than rejected, so a webhook-configured bot
    # against this server fails to *receive* updates but not on this call.
    return True


async def _delete_webhook(_s: SandboxStore, _payload: dict[str, Any]) -> bool:
    return True


async def _get_webhook_info(_s: SandboxStore, _payload: dict[str, Any]) -> dict[str, Any]:
    # Always "no webhook configured" — which is exactly what a long-polling
    # bot gets from the real server.
    return {"url": "", "has_custom_certificate": False, "pending_update_count": 0}


def _emit_message(
    s: SandboxStore,
    payload: dict[str, Any],
    *,
    text: str | None,
    media: str | None,
    entities: list[dict[str, Any]] | None = None,
    caption: str | None = None,
    caption_entities: list[dict[str, Any]] | None = None,
    media_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    chat = _require_chat(s, _chat_ref(payload))
    reply_to_message_id, allow_without_reply = _resolve_reply_to(payload)
    if (
        reply_to_message_id is not None
        and not allow_without_reply
        and s.message(chat.id, reply_to_message_id) is None
    ):
        raise TelegramApiError(400, "Bad Request: message to reply not found")
    message = SandboxMessage(
        message_id=s.next_message_id(),
        chat_id=chat.id,
        from_id=bot_id(),
        text=text,
        date=time.time(),
        reply_to_message_id=reply_to_message_id,
        entities=entities or [],
        reply_markup=_json_field(payload, "reply_markup"),
        media=media,
        media_file_id=_resolve_media_file(s, payload, media) if media else None,
        media_caption=caption,
        caption_entities=caption_entities or [],
        link_preview_options=_json_field(payload, "link_preview_options"),
        message_thread_id=_int(payload, "message_thread_id"),
        media_extra=media_extra or {},
    )
    s.add_message(message)
    rendered = message.as_telegram(s)
    s.publish("message", rendered)
    return rendered


#: Which payload field carries the file for each media kind — the Bot API
#: names the parameter after the kind, except `animation`, whose field is
#: `animation` but whose kind on the resulting message is also `animation`.
_MEDIA_FIELD: dict[str, str] = {
    "photo": "photo",
    "sticker": "sticker",
    "video": "video",
    "animation": "animation",
    "document": "document",
    "audio": "audio",
    "voice": "voice",
}


def _resolve_media_file(s: SandboxStore, payload: dict[str, Any], media: str) -> str | None:
    """Which stored blob this outbound media message is, if any.

    Three cases, all real:

    * The bot uploaded bytes (`InputFile`) — store them, and the tester sees
      the actual picture the bot generated. This is the case that matters:
      a captcha image, a chart, a resized thumbnail are all things you can
      only validate by looking at them.
    * The bot passed a `file_id` this sandbox already has — reuse it, which is
      also what real Telegram does, and what makes "re-send the photo you were
      given" observably work rather than silently degrade.
    * The bot passed an id from somewhere else, or a URL. Nothing to show; the
      message still renders as a labelled placeholder rather than pretending.
    """
    field = _MEDIA_FIELD.get(media)
    if field is None:
        return None

    uploads = payload.get(_UPLOADS_KEY)
    if isinstance(uploads, dict) and field in uploads:
        filename, data = uploads[field]
        try:
            return s.store_file(data, file_name=filename).file_id
        except ValueError as exc:
            # Oversized or malformed: the send itself is not the thing under
            # test, so it proceeds without bytes rather than failing with an
            # error real Telegram would not have given.
            log.warning("sandbox.files.upload_rejected", method=media, error=str(exc))
            return None

    reference = payload.get(field)
    if isinstance(reference, str) and reference in s.files:
        return reference
    return None


async def _send_message(s: SandboxStore, payload: dict[str, Any]) -> dict[str, Any]:
    text, entities = _formatted_text(payload, "text", "entities")
    if not text:
        raise TelegramApiError(400, "Bad Request: message text is empty")
    return _emit_message(s, payload, text=text, media=None, entities=entities)


def _captioned_media(
    media: str,
) -> Callable[[SandboxStore, dict[str, Any]], Awaitable[dict[str, Any]]]:
    """`sendPhoto`/`sendVideo`/`sendAnimation`/`sendDocument`/`sendAudio`/
    `sendVoice` differ only in which Bot API method name maps to which
    `media` tag — one factory instead of six near-identical `async def`s."""

    async def handler(s: SandboxStore, payload: dict[str, Any]) -> dict[str, Any]:
        caption, caption_entities = _formatted_text(payload, "caption", "caption_entities")
        return _emit_message(
            s, payload, text=None, media=media, caption=caption, caption_entities=caption_entities
        )

    return handler


async def _send_sticker(s: SandboxStore, payload: dict[str, Any]) -> dict[str, Any]:
    # Real `sendSticker` has no caption parameter; kept out on purpose.
    return _emit_message(s, payload, text=None, media="sticker")


async def _send_dice(s: SandboxStore, payload: dict[str, Any]) -> dict[str, Any]:
    emoji = _str(payload, "emoji", "🎲") or "🎲"
    value = random.randint(1, _DICE_RANGES.get(emoji, 6))
    return _emit_message(
        s, payload, text=None, media="dice", media_extra={"emoji": emoji, "value": value}
    )


async def _send_chat_action(s: SandboxStore, payload: dict[str, Any]) -> bool:
    _require_chat(s, _chat_ref(payload))
    return True


async def _edit_message_text(s: SandboxStore, payload: dict[str, Any]) -> dict[str, Any]:
    message = _require_message(
        s, _require_int(payload, "chat_id"), _require_int(payload, "message_id"), purpose="edit"
    )
    text, entities = _formatted_text(payload, "text", "entities")
    if not text:
        raise TelegramApiError(400, "Bad Request: message text is empty")
    message.text = text
    message.entities = entities
    reply_markup = _json_field(payload, "reply_markup")
    if reply_markup is not None:
        message.reply_markup = reply_markup
    message.edited = True
    rendered = message.as_telegram(s)
    s.publish("edit", rendered)
    return rendered


async def _edit_message_caption(s: SandboxStore, payload: dict[str, Any]) -> dict[str, Any]:
    message = _require_message(
        s, _require_int(payload, "chat_id"), _require_int(payload, "message_id"), purpose="edit"
    )
    caption, caption_entities = _formatted_text(payload, "caption", "caption_entities")
    message.media_caption = caption
    message.caption_entities = caption_entities
    reply_markup = _json_field(payload, "reply_markup")
    if reply_markup is not None:
        message.reply_markup = reply_markup
    message.edited = True
    rendered = message.as_telegram(s)
    s.publish("edit", rendered)
    return rendered


async def _edit_message_reply_markup(s: SandboxStore, payload: dict[str, Any]) -> dict[str, Any]:
    message = _require_message(
        s, _require_int(payload, "chat_id"), _require_int(payload, "message_id"), purpose="edit"
    )
    message.reply_markup = _json_field(payload, "reply_markup")
    message.edited = True
    rendered = message.as_telegram(s)
    s.publish("edit", rendered)
    return rendered


async def _delete_message(s: SandboxStore, payload: dict[str, Any]) -> bool:
    chat_id = _require_int(payload, "chat_id")
    message_id = _require_int(payload, "message_id")
    message = _require_message(s, chat_id, message_id, purpose="delete")
    message.deleted = True
    s.publish("delete", {"chat_id": chat_id, "message_id": message_id})
    return True


async def _delete_messages(s: SandboxStore, payload: dict[str, Any]) -> bool:
    chat = _require_chat(s, _chat_ref(payload))
    message_ids = _json_field(payload, "message_ids")
    if not isinstance(message_ids, list) or not message_ids:
        raise TelegramApiError(400, "Bad Request: message_ids is required")
    # Real Telegram skips ids it can't find rather than failing the whole
    # call — deliberately not an error here either.
    deleted: list[int] = []
    for raw_id in message_ids:
        message = s.message(chat.id, int(raw_id))
        if message is not None:
            message.deleted = True
            deleted.append(message.message_id)
    s.publish("delete", {"chat_id": chat.id, "message_ids": deleted})
    return True


async def _forward_message(s: SandboxStore, payload: dict[str, Any]) -> dict[str, Any]:
    target = _require_chat(s, _chat_ref(payload, "chat_id"))
    source = _require_chat(s, _chat_ref(payload, "from_chat_id"))
    original = _require_message(
        s, source.id, _require_int(payload, "message_id"), purpose="forward"
    )
    sender = s.users.get(original.from_id)
    sender_payload = (
        sender.as_telegram()
        if sender
        else {"id": original.from_id, "is_bot": False, "first_name": "User"}
    )
    forwarded = SandboxMessage(
        message_id=s.next_message_id(),
        chat_id=target.id,
        # `from` on a forwarded message is whoever called the Bot API (the
        # bot) — `forward_origin` is what carries the original attribution.
        from_id=bot_id(),
        text=original.text,
        date=time.time(),
        entities=list(original.entities),
        media=original.media,
        media_caption=original.media_caption,
        caption_entities=list(original.caption_entities),
        media_extra=dict(original.media_extra),
        forward_origin={"type": "user", "date": int(original.date), "sender_user": sender_payload},
    )
    s.add_message(forwarded)
    rendered = forwarded.as_telegram(s)
    s.publish("message", rendered)
    return rendered


async def _copy_message(s: SandboxStore, payload: dict[str, Any]) -> dict[str, Any]:
    """Unlike `forwardMessage`, real `copyMessage` sends a genuinely new
    message with no forward attribution at all, and returns only a
    `MessageId` (`{"message_id": ...}`), not a full `Message`."""
    target = _require_chat(s, _chat_ref(payload, "chat_id"))
    source = _require_chat(s, _chat_ref(payload, "from_chat_id"))
    original = _require_message(s, source.id, _require_int(payload, "message_id"), purpose="copy")
    if _str(payload, "caption") is not None:
        caption, caption_entities = _formatted_text(payload, "caption", "caption_entities")
    else:
        caption, caption_entities = original.media_caption, list(original.caption_entities)
    copied = SandboxMessage(
        message_id=s.next_message_id(),
        chat_id=target.id,
        from_id=bot_id(),
        text=original.text,
        date=time.time(),
        entities=list(original.entities),
        media=original.media,
        media_caption=caption,
        caption_entities=caption_entities,
        media_extra=dict(original.media_extra),
    )
    s.add_message(copied)
    rendered = copied.as_telegram(s)
    s.publish("message", rendered)
    return {"message_id": copied.message_id}


async def _answer_callback_query(s: SandboxStore, payload: dict[str, Any]) -> bool:
    query_id = _require_str(payload, "callback_query_id")
    if not s.consume_callback_query(query_id):
        raise TelegramApiError(
            400, "Bad Request: query is too old and response timeout expired or query id is invalid"
        )
    s.publish(
        "callback_answer",
        {
            "callback_query_id": query_id,
            "text": _str(payload, "text"),
            "show_alert": _bool(payload, "show_alert", False),
        },
    )
    return True


async def _get_chat(s: SandboxStore, payload: dict[str, Any]) -> dict[str, Any]:
    chat = _require_chat(s, _chat_ref(payload))
    return chat.as_telegram()


async def _get_chat_member_count(s: SandboxStore, payload: dict[str, Any]) -> int:
    chat = _require_chat(s, _chat_ref(payload))
    return sum(1 for member in chat.members.values() if member.role not in ("left", "kicked"))


def _chat_member_payload(s: SandboxStore, membership: Membership) -> dict[str, Any]:
    """Every field aiogram's `ChatMember*` union marks required for the
    member's `status`, built from the dataclass state.py actually tracks
    (`role`, `anonymous`, `restricted_until`, `permissions`). This is the
    payload `test_telegram_api.py` validates against the real aiogram models.
    """
    user = s.users.get(membership.user_id)
    user_payload = (
        user.as_telegram()
        if user is not None
        else {"id": membership.user_id, "is_bot": False, "first_name": f"User{membership.user_id}"}
    )
    until_date = int(membership.restricted_until) if membership.restricted_until else 0
    if membership.role == "creator":
        return {"status": "creator", "user": user_payload, "is_anonymous": membership.anonymous}
    if membership.role == "administrator":
        return {
            "status": "administrator",
            "user": user_payload,
            "is_anonymous": membership.anonymous,
            "can_be_edited": False,
            "can_manage_chat": True,
            "can_delete_messages": True,
            "can_manage_video_chats": True,
            "can_restrict_members": True,
            "can_promote_members": False,
            "can_change_info": True,
            "can_invite_users": True,
            "can_post_stories": False,
            "can_edit_stories": False,
            "can_delete_stories": False,
        }
    if membership.role == "restricted":
        perms = membership.permissions
        return {
            "status": "restricted",
            "user": user_payload,
            "is_member": True,
            "until_date": until_date,
            **{key: perms.get(key, False) for key in _PERMISSION_KEYS},
        }
    if membership.role == "kicked":
        return {"status": "kicked", "user": user_payload, "until_date": until_date}
    if membership.role == "left":
        return {"status": "left", "user": user_payload}
    return {"status": "member", "user": user_payload}


async def _get_chat_member(s: SandboxStore, payload: dict[str, Any]) -> dict[str, Any]:
    chat = _require_chat(s, _chat_ref(payload))
    user_id = _require_int(payload, "user_id")
    membership = chat.members.get(user_id)
    if membership is None:
        raise TelegramApiError(400, "Bad Request: user not found")
    return _chat_member_payload(s, membership)


async def _get_chat_administrators(
    s: SandboxStore, payload: dict[str, Any]
) -> list[dict[str, Any]]:
    chat = _require_chat(s, _chat_ref(payload))
    return [
        _chat_member_payload(s, member)
        for member in chat.members.values()
        if member.role in ("creator", "administrator")
    ]


async def _restrict_chat_member(s: SandboxStore, payload: dict[str, Any]) -> bool:
    chat = _require_chat(s, _chat_ref(payload))
    user_id = _require_int(payload, "user_id")
    until_date = _normalize_until_date(_int(payload, "until_date", 0) or 0)
    independent = _bool(payload, "use_independent_chat_permissions", False)
    permissions = _normalize_permissions(_json_field(payload, "permissions") or {}, independent)
    member = chat.members.setdefault(user_id, Membership(user_id=user_id))
    member.permissions = permissions
    # A restriction that grants every permission back is Telegram's own way of
    # lifting one (v1's `welcome_message` calls `restrictChatMember` twice:
    # once wide open, then immediately re-narrowed) — mirrored here rather
    # than treated as "still restricted".
    member.role = "member" if all(permissions.values()) else "restricted"
    member.restricted_until = until_date if member.role == "restricted" else 0.0
    s.publish("member", {"chat_id": chat.id, "user_id": user_id, "role": member.role})
    return True


async def _ban_chat_member(s: SandboxStore, payload: dict[str, Any]) -> bool:
    chat = _require_chat(s, _chat_ref(payload))
    user_id = _require_int(payload, "user_id")
    until_date = _normalize_until_date(_int(payload, "until_date", 0) or 0)
    member = chat.members.setdefault(user_id, Membership(user_id=user_id))
    member.role = "kicked"
    member.restricted_until = until_date
    s.publish("member", {"chat_id": chat.id, "user_id": user_id, "role": "kicked"})
    return True


async def _unban_chat_member(s: SandboxStore, payload: dict[str, Any]) -> bool:
    chat = _require_chat(s, _chat_ref(payload))
    user_id = _require_int(payload, "user_id")
    only_if_banned = _bool(payload, "only_if_banned", False)
    member = chat.members.get(user_id)
    # `only_if_banned=True`: lift a ban if there is one, otherwise leave the
    # member untouched. The default (`False`) is real Telegram's own
    # "unconditionally reset to left" behaviour, whether or not they were
    # ever banned — which is what the pre-existing tests below exercise.
    if only_if_banned and (member is None or member.role != "kicked"):
        return True
    if member is not None:
        member.role = "left"
        member.restricted_until = 0.0
    s.publish("member", {"chat_id": chat.id, "user_id": user_id, "role": "left"})
    return True


_PROMOTION_FLAGS = (
    "can_manage_chat",
    "can_delete_messages",
    "can_manage_video_chats",
    "can_restrict_members",
    "can_promote_members",
    "can_change_info",
    "can_invite_users",
    "can_post_messages",
    "can_edit_messages",
    "can_pin_messages",
)


async def _promote_chat_member(s: SandboxStore, payload: dict[str, Any]) -> bool:
    chat = _require_chat(s, _chat_ref(payload))
    user_id = _require_int(payload, "user_id")
    member = chat.members.setdefault(user_id, Membership(user_id=user_id))
    # Telegram also uses `promoteChatMember` to demote (every flag `False`);
    # any granted flag means "now an admin", none means "back to member".
    is_promotion = any(_bool(payload, flag, False) for flag in _PROMOTION_FLAGS)
    member.role = "administrator" if is_promotion else "member"
    s.publish("member", {"chat_id": chat.id, "user_id": user_id, "role": member.role})
    return True


async def _set_chat_permissions(s: SandboxStore, payload: dict[str, Any]) -> bool:
    chat = _require_chat(s, _chat_ref(payload))
    independent = _bool(payload, "use_independent_chat_permissions", False)
    chat.default_permissions = _normalize_permissions(
        _json_field(payload, "permissions") or {}, independent
    )
    s.publish("chat", {"chat_id": chat.id, "permissions": chat.default_permissions})
    return True


async def _pin_chat_message(s: SandboxStore, payload: dict[str, Any]) -> bool:
    chat = _require_chat(s, _chat_ref(payload))
    message_id = _require_int(payload, "message_id")
    if s.message(chat.id, message_id) is None:
        raise TelegramApiError(400, "Bad Request: message to pin not found")
    chat.pinned_message_id = message_id
    s.publish("pin", {"chat_id": chat.id, "message_id": message_id})
    return True


async def _unpin_chat_message(s: SandboxStore, payload: dict[str, Any]) -> bool:
    chat = _require_chat(s, _chat_ref(payload))
    message_id = _int(payload, "message_id")
    # An omitted `message_id` unpins whatever is currently pinned. The sandbox
    # tracks only the single most recent pin (see `SandboxChat.pinned_message_id`),
    # so an explicit id that doesn't match it is simply a no-op, same as real
    # Telegram unpinning a message that was never pinned.
    if message_id is None or chat.pinned_message_id == message_id:
        chat.pinned_message_id = None
    s.publish("pin", {"chat_id": chat.id, "message_id": None})
    return True


async def _leave_chat(s: SandboxStore, payload: dict[str, Any]) -> bool:
    chat = _require_chat(s, _chat_ref(payload))
    member = chat.members.get(bot_id())
    if member is not None:
        member.role = "left"
    s.publish("member", {"chat_id": chat.id, "user_id": bot_id(), "role": "left"})
    return True


async def _set_chat_title(s: SandboxStore, payload: dict[str, Any]) -> bool:
    chat = _require_chat(s, _chat_ref(payload))
    chat.title = _require_str(payload, "title")
    s.publish("chat", {"chat_id": chat.id, "title": chat.title})
    return True


async def _set_chat_description(s: SandboxStore, payload: dict[str, Any]) -> bool:
    chat = _require_chat(s, _chat_ref(payload))
    chat.description = _str(payload, "description")
    s.publish("chat", {"chat_id": chat.id, "description": chat.description})
    return True


async def _export_chat_invite_link(s: SandboxStore, payload: dict[str, Any]) -> str:
    chat = _require_chat(s, _chat_ref(payload))
    return f"https://t.me/+sandbox-{chat.id}"


async def _set_message_reaction(s: SandboxStore, payload: dict[str, Any]) -> bool:
    chat = _require_chat(s, _chat_ref(payload))
    message_id = _require_int(payload, "message_id")
    _require_message(s, chat.id, message_id, purpose="react to")
    reaction = _json_field(payload, "reaction") or []
    # Real Telegram aggregates reactions from every user into `Message.reactions`
    # (a `ReactionCount` list) — modelling that aggregate has no UAT payoff
    # here, so the sandbox only records that the call happened (the API-call
    # log is the point of this method for a human driving the sandbox) rather
    # than mutating the message's own rendered shape. See the README.
    s.publish("reaction", {"chat_id": chat.id, "message_id": message_id, "reaction": reaction})
    return True


def _commands_scope_key(payload: dict[str, Any]) -> tuple[str, str]:
    scope = _json_field(payload, "scope")
    scope_key = (
        json.dumps(scope, sort_keys=True) if isinstance(scope, dict) else '{"type": "default"}'
    )
    return scope_key, _str(payload, "language_code", "") or ""


async def _set_my_commands(s: SandboxStore, payload: dict[str, Any]) -> bool:
    commands = _json_field(payload, "commands")
    if not isinstance(commands, list):
        raise TelegramApiError(400, "Bad Request: commands is required")
    s.bot_commands[_commands_scope_key(payload)] = commands
    return True


async def _get_my_commands(s: SandboxStore, payload: dict[str, Any]) -> list[dict[str, Any]]:
    scope_key, language_code = _commands_scope_key(payload)
    default_key = '{"type": "default"}'
    # Real Telegram's own fallback chain (specific scope > default scope,
    # then language-specific > language-agnostic) collapsed to its two axes —
    # enough to answer this method's actual UAT question, "what did
    # setMyCommands leave in place for this chat/language".
    for key in (
        (scope_key, language_code),
        (scope_key, ""),
        (default_key, language_code),
        (default_key, ""),
    ):
        if key in s.bot_commands:
            return s.bot_commands[key]
    return []


async def _delete_my_commands(s: SandboxStore, payload: dict[str, Any]) -> bool:
    s.bot_commands.pop(_commands_scope_key(payload), None)
    return True


async def _get_file(s: SandboxStore, payload: dict[str, Any]) -> dict[str, Any]:
    """Real size and a real download path for anything this sandbox stored.

    A bot that downloads what it was sent — to hash it, to check its
    dimensions, to hand it to a storage backend — gets the actual bytes back
    from the path this returns. Before the file store existed, every download
    produced the same 27-byte placeholder, so any of those checks passed or
    failed for reasons that had nothing to do with the image.
    """
    file_id = _require_str(payload, "file_id")
    stored = s.files.get(file_id)
    if stored is None:
        return {
            "file_id": file_id,
            "file_unique_id": f"u-{file_id}",
            "file_size": len(_PLACEHOLDER_FILE_BYTES),
            "file_path": f"sandbox/{file_id}",
        }
    return {
        "file_id": stored.file_id,
        "file_unique_id": stored.file_unique_id,
        "file_size": stored.size,
        "file_path": f"sandbox/{stored.file_id}",
    }


_METHODS: dict[str, Callable[[SandboxStore, dict[str, Any]], Awaitable[Any]]] = {
    "getMe": _get_me,
    "getUpdates": _get_updates,
    "setWebhook": _set_webhook,
    "deleteWebhook": _delete_webhook,
    "getWebhookInfo": _get_webhook_info,
    "sendMessage": _send_message,
    "sendPhoto": _captioned_media("photo"),
    "sendVideo": _captioned_media("video"),
    "sendAnimation": _captioned_media("animation"),
    "sendDocument": _captioned_media("document"),
    "sendAudio": _captioned_media("audio"),
    "sendVoice": _captioned_media("voice"),
    "sendSticker": _send_sticker,
    "sendDice": _send_dice,
    "sendChatAction": _send_chat_action,
    "editMessageText": _edit_message_text,
    "editMessageCaption": _edit_message_caption,
    "editMessageReplyMarkup": _edit_message_reply_markup,
    "deleteMessage": _delete_message,
    "deleteMessages": _delete_messages,
    "forwardMessage": _forward_message,
    "copyMessage": _copy_message,
    "answerCallbackQuery": _answer_callback_query,
    "getChat": _get_chat,
    "getChatMemberCount": _get_chat_member_count,
    "getChatMember": _get_chat_member,
    "getChatAdministrators": _get_chat_administrators,
    "restrictChatMember": _restrict_chat_member,
    "banChatMember": _ban_chat_member,
    "unbanChatMember": _unban_chat_member,
    "promoteChatMember": _promote_chat_member,
    "setChatPermissions": _set_chat_permissions,
    "pinChatMessage": _pin_chat_message,
    "unpinChatMessage": _unpin_chat_message,
    "leaveChat": _leave_chat,
    "setChatTitle": _set_chat_title,
    "setChatDescription": _set_chat_description,
    "exportChatInviteLink": _export_chat_invite_link,
    "setMessageReaction": _set_message_reaction,
    "setMyCommands": _set_my_commands,
    "getMyCommands": _get_my_commands,
    "deleteMyCommands": _delete_my_commands,
    "getFile": _get_file,
}


# ----------------------------------------------------------------- dispatch


def _telegram_error(
    status_code: int, description: str, *, parameters: dict[str, Any] | None = None
) -> JSONResponse:
    body: dict[str, Any] = {"ok": False, "error_code": status_code, "description": description}
    if parameters:
        body["parameters"] = parameters
    return JSONResponse(body, status_code=status_code)


@router.api_route("/bot{token}/{method}", methods=["GET", "POST"])
async def call_method(token: str, method: str, request: Request) -> JSONResponse:
    s = store()
    _ensure_bot_user(s)
    payload = await _extract_payload(request)
    # Recorded before dispatch: a call that goes on to fail is still a call the
    # bot made, and the "what did the bot actually do" log is exactly where
    # that should be visible. Uploaded bytes are stripped first — the log is
    # read by a human and serialised to JSON, and neither wants a megabyte of
    # binary in it. The filename stays, under the parameter's own name.
    s.record_api_call(method, {k: v for k, v in payload.items() if k != _UPLOADS_KEY})
    handler = _METHODS.get(method)
    if handler is None:
        # The real server's exact wording — verified against the tdlib
        # self-hosted implementation, not paraphrased — for both a genuinely
        # unknown method name and a real Bot API method this sandbox has not
        # implemented (payments, inline mode, stickers management, business
        # accounts; see the README's divergence table). The two are indistinguishable from
        # here, which is itself a documented divergence: a real, valid method
        # this file hasn't implemented yet 404s instead of getting whatever
        # method-specific error the real server would give it.
        return _telegram_error(404, "Not Found: method not found")
    try:
        result = await handler(s, payload)
    except TelegramApiError as exc:
        return _telegram_error(exc.status_code, exc.description, parameters=exc.parameters)
    return JSONResponse({"ok": True, "result": result})


@router.get("/file/bot{token}/{file_path:path}")
async def download_file(token: str, file_path: str) -> Response:
    """The second half of `getFile`/`bot.download()`: a client fetches the
    `file_path` `getFile` returned from exactly this URL shape.

    Serves the stored bytes with their sniffed content type, so a handler that
    content-sniffs a download sees what it would see against real Telegram. An
    unknown path still returns the placeholder rather than a 404: failing the
    download would break the handler under test for a reason that is about the
    sandbox, not about the handler.
    """
    stored = store().files.get(file_path.rsplit("/", 1)[-1])
    if stored is None:
        return Response(content=_PLACEHOLDER_FILE_BYTES, media_type="application/octet-stream")
    return Response(content=stored.data, media_type=stored.mime_type)
