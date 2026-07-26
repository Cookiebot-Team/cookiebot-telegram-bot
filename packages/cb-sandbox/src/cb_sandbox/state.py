"""The sandbox's world: users, chats, messages, and the update queue.

This is the contract the two API surfaces share. `telegram_api.py` serves it to
the bot in Telegram's own shapes; `control_api.py` serves it to the web client
and the test kit in whatever shape is convenient. Neither owns the state.

Why a queue rather than webhooks: every Bot API client already speaks long
polling, so pointing a bot's API base at this server makes it poll the sandbox
with **no change to the bot** — no webhook to register, no tunnel, no TLS. What
you click in the UI drives the same handler stack that production runs.

Everything here is a live, in-memory read path on purpose — every lookup the
two API surfaces make goes through these dicts, never through DuckDB. But the
process dying used to mean the scenario died with it, which made this tool
useless for anything a second process needed to see (the web UI server after
a restart, a test run). `persistence.py` is the durable copy: every mutation
below also lands in a DuckDB file, and `SandboxStore.load()` restores it at
startup. `reset()` is still meant to be pressed constantly — it now clears
both.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from collections.abc import MutableMapping
from dataclasses import dataclass, field
from typing import Any, Literal

from cb_sandbox.config import get_config
from cb_sandbox.files import PLACEHOLDER_FILE_IDS, FileStore, SandboxFile
from cb_sandbox.logging import get_logger
from cb_sandbox.persistence import SandboxDB

log = get_logger("cb.sandbox.state")

#: Telegram's fixed id for a message sent by an admin with anonymity switched on.
ANONYMOUS_BOT_ID = 1087968824

#: The media kinds backed by an actual file (everything except `dice`, whose
#: "media" is a server-side roll with no bytes behind it).
_FILE_MEDIA_KINDS: frozenset[str] = frozenset(
    {"photo", "sticker", "video", "animation", "document", "audio", "voice"}
)

#: Stand-in size for a media message with no stored bytes, so `file_size` is
#: never absent from a payload whose model marks it optional-but-expected.
_PLACEHOLDER_BYTES = b"cb-sandbox-placeholder-file"

ChatType = Literal["private", "group", "supergroup"]
Role = Literal["creator", "administrator", "member", "restricted", "kicked", "left"]


@dataclass(slots=True)
class SandboxUser:
    id: int
    first_name: str
    username: str
    last_name: str | None = None
    language_code: str = "en"
    is_bot: bool = False

    def as_telegram(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "is_bot": self.is_bot,
            "first_name": self.first_name,
            "username": self.username,
            "language_code": self.language_code,
        }
        if self.last_name:
            payload["last_name"] = self.last_name
        return payload


@dataclass(slots=True)
class Membership:
    user_id: int
    role: Role = "member"
    #: The admin's own "remain anonymous" toggle — the single most valuable
    #: thing this sandbox can reproduce by hand, because an anonymous admin's
    #: message arrives `from` GroupAnonymousBot with the group in
    #: `sender_chat`, and an admin check written against `from` gets it wrong.
    anonymous: bool = False
    joined_at: float = field(default_factory=time.time)
    restricted_until: float = 0.0
    #: The exact `ChatPermissions` last applied by `restrictChatMember`, after
    #: `use_independent_chat_permissions` normalisation — what
    #: `getChatMember`'s "restricted" branch renders back. Without this, every
    #: restricted member rendered the same fixed set of flags regardless of
    #: what was actually restricted, which is wrong the moment a caller (or a
    #: test) restricts anything other than the exact combination the old
    #: hardcoded template happened to use.
    permissions: dict[str, bool] = field(default_factory=dict)


@dataclass(slots=True)
class SandboxChat:
    id: int
    title: str
    type: ChatType = "supergroup"
    #: Set only for a chat that would be publicly addressable as `@name` on
    #: real Telegram — resolved by `SandboxStore.chat_by_username` for the
    #: `chat_id: "@username"` form `sendMessage` and friends accept.
    username: str | None = None
    description: str | None = None
    #: `pinChatMessage`/`unpinChatMessage` track only the single most recently
    #: pinned message — real Telegram allows several simultaneously pinned
    #: messages with only the latest surfaced on the `Chat` object; modelling
    #: more than that has no UAT payoff here. Deliberately not rendered back
    #: through `as_telegram()`: doing so would mean embedding a full `Message`,
    #: which embeds its own `chat`, which would recurse into this field again.
    pinned_message_id: int | None = None
    #: The chat-wide default set by `setChatPermissions`, independent of any
    #: per-member override `restrictChatMember` applies.
    default_permissions: dict[str, bool] = field(default_factory=dict)
    #: A plain dict at construction time — `SandboxChat(...)` is built with no
    #: store in scope to bind it to. `_ChatRegistry.__setitem__` upgrades it to
    #: a `_MemberRegistry` the moment the chat is registered with a store, so
    #: every membership added afterwards (the only time one ever is, in every
    #: caller) persists without either caller needing to know that.
    members: MutableMapping[int, Membership] = field(default_factory=dict)

    def as_telegram(self) -> dict[str, Any]:
        # A private chat has no `title` on real Telegram — a DM peer is
        # identified by `first_name`/`username`, same as any other `User`.
        # `self.title` is still where `control_api.py` stores the peer's
        # display name (there is no separate field for it), so it is the
        # right source, just under the field real Telegram actually sends.
        if self.type == "private":
            payload: dict[str, Any] = {"id": self.id, "type": "private", "first_name": self.title}
        else:
            payload = {"id": self.id, "type": self.type, "title": self.title}
        if self.username:
            payload["username"] = self.username
        if self.description:
            payload["description"] = self.description
        return payload

    def admin_ids(self) -> list[int]:
        return [m.user_id for m in self.members.values() if m.role in ("creator", "administrator")]


@dataclass(slots=True)
class SandboxMessage:
    """One message as the UI needs it, with enough to rebuild the Telegram shape."""

    message_id: int
    chat_id: int
    from_id: int
    text: str | None
    date: float
    #: Set when an admin posted anonymously: Telegram replaces the sender with
    #: GroupAnonymousBot and attaches `sender_chat` = the group.
    sender_chat_id: int | None = None
    reply_to_message_id: int | None = None
    #: Real Telegram entities parsed out of `parse_mode` markup by
    #: `telegram_api._apply_parse_mode` — `text` itself is always the plain
    #: string real Telegram would store, never raw HTML/MarkdownV2 source.
    entities: list[dict[str, Any]] = field(default_factory=list)
    reply_markup: dict[str, Any] | None = None
    #: "photo" | "sticker" | "video" | "animation" | "document" | "audio" |
    #: "voice" | "dice" | None — what the UI renders and `as_telegram` shapes.
    media: str | None = None
    media_caption: str | None = None
    #: Entities parsed out of the *caption's* `parse_mode`/`caption_entities` —
    #: a distinct field on real Telegram from the message-text `entities`
    #: above, and both can be non-empty on the same message (e.g. a photo with
    #: a formatted caption has only `caption_entities`, never `entities`).
    caption_entities: list[dict[str, Any]] = field(default_factory=list)
    edited: bool = False
    deleted: bool = False
    link_preview_options: dict[str, Any] | None = None
    message_thread_id: int | None = None
    #: Set only by `forwardMessage` — a `MessageOrigin*` object. `from` on a
    #: forwarded message is still the bot (whoever called the Bot API method),
    #: never the original sender; this is what carries the original
    #: attribution, exactly as real Telegram splits the two.
    forward_origin: dict[str, Any] | None = None
    #: Which stored blob this message's media actually is
    #: (`cb_sandbox.files.FileStore`). `None` means the media has no bytes here
    #: — a bot re-sending a `file_id` minted by production, or a fixture that
    #: only cares that *a* photo was sent. The client draws those as a labelled
    #: placeholder rather than a broken image, which is the honest rendering:
    #: "a photo, contents unknown" is a different fact from "a photo".
    media_file_id: str | None = None
    #: Small per-media-type extras that don't warrant their own column —
    #: today only a dice roll's `emoji`/`value`.
    media_extra: dict[str, Any] = field(default_factory=dict)
    #: A membership service message: `{"kind": "join"|"leave", "user_id": N}`.
    #: Telegram models these as ordinary messages carrying `new_chat_members` /
    #: `left_chat_member` instead of `text`, and they must be *stored* like
    #: ordinary messages too — the captcha replies to the join message, and a
    #: reply needs a message to point at. Keeping the discriminator here rather
    #: than in `control_api` means the Telegram shape is rebuilt from storage on
    #: reload, exactly like every other message.
    service: dict[str, Any] | None = None
    #: Which `SandboxScenario` was active when this message was recorded —
    #: stamped once, by `SandboxStore.add_message`, from `store.active_scenario_id`
    #: at that exact moment. Never touched again afterwards, including if the
    #: scenario is later renamed or closed: a message belongs to whatever was
    #: running when it happened, not to whatever is running when someone reads
    #: it back. `None` for anything recorded with no scenario active — most
    #: traffic, until a caller opts in.
    scenario_id: str | None = None

    def as_telegram(self, store: SandboxStore) -> dict[str, Any]:
        sender = store.users.get(self.from_id)
        payload: dict[str, Any] = {
            "message_id": self.message_id,
            "date": int(self.date),
            "chat": store.chats[self.chat_id].as_telegram(),
            "from": (sender.as_telegram() if sender else {"id": self.from_id, "is_bot": False}),
        }
        if self.sender_chat_id is not None:
            payload["sender_chat"] = store.chats[self.sender_chat_id].as_telegram()
        if self.forward_origin is not None:
            payload["forward_origin"] = self.forward_origin
        if self.message_thread_id is not None:
            payload["message_thread_id"] = self.message_thread_id
            payload["is_topic_message"] = True
        if self.text is not None:
            payload["text"] = self.text
            payload["entities"] = self.entities
        if self.link_preview_options is not None:
            payload["link_preview_options"] = self.link_preview_options
        if self.reply_to_message_id is not None:
            replied = store.message(self.chat_id, self.reply_to_message_id)
            if replied is not None:
                payload["reply_to_message"] = replied.as_telegram(store)
        if self.media in _FILE_MEDIA_KINDS:
            payload.update(self._media_payload(store))
        elif self.media == "dice":
            payload["dice"] = {
                "emoji": self.media_extra.get("emoji", "🎲"),
                "value": self.media_extra.get("value", 1),
            }
        if self.media_caption:
            payload["caption"] = self.media_caption
            if self.caption_entities:
                payload["caption_entities"] = self.caption_entities
        if self.reply_markup:
            payload["reply_markup"] = self.reply_markup
        if self.service is not None:
            kind = self.service.get("kind")
            subject_id = self.service.get("user_id")
            subject = store.users.get(subject_id) if isinstance(subject_id, int) else None
            if subject is not None and kind == "join":
                payload["new_chat_members"] = [subject.as_telegram()]
            elif subject is not None and kind == "leave":
                payload["left_chat_member"] = subject.as_telegram()
        return payload

    def _media_payload(self, store: SandboxStore) -> dict[str, Any]:
        """The `photo`/`sticker`/`video`/... field, described from the real
        bytes where there are any.

        Dimensions, mime type and size are read off the stored file rather than
        hardcoded, because they are exactly what an image-handling feature
        branches on: "reject anything over N pixels", "only accept image/*",
        "resize if wider than the thumbnail". A sandbox that answered 640x480
        for every picture would let all of that pass untested.

        A message with no stored file still renders a well-formed payload with
        the placeholder id — the bot must never receive a `photo` array with a
        missing `file_id`, which its own model would reject outright.
        """
        kind = self.media or ""
        stored = store.files.get(self.media_file_id) if self.media_file_id else None
        file_id = self.media_file_id or PLACEHOLDER_FILE_IDS.get(kind, f"sandbox-{kind}")
        unique_id = stored.file_unique_id if stored else f"u-{file_id}"

        if kind == "photo":
            # Real Telegram sends several sizes; one entry is a legitimate
            # (and common) response, and the last element is the largest,
            # which is what `message.photo[-1]` in every handler reads.
            return {
                "photo": [
                    {
                        "file_id": file_id,
                        "file_unique_id": unique_id,
                        "width": stored.width if stored and stored.width else 640,
                        "height": stored.height if stored and stored.height else 480,
                        "file_size": stored.size if stored else len(_PLACEHOLDER_BYTES),
                    }
                ]
            }
        if kind == "sticker":
            return {
                "sticker": {
                    "file_id": file_id,
                    "file_unique_id": unique_id,
                    "width": stored.width if stored and stored.width else 512,
                    "height": stored.height if stored and stored.height else 512,
                    "is_animated": False,
                    "is_video": bool(stored and stored.mime_type.startswith("video/")),
                    "type": "regular",
                    **({"file_size": stored.size} if stored else {}),
                }
            }
        if kind in ("video", "animation"):
            return {
                kind: {
                    "file_id": file_id,
                    "file_unique_id": unique_id,
                    "width": stored.width if stored and stored.width else 640,
                    "height": stored.height if stored and stored.height else 480,
                    "duration": stored.duration if stored else 3,
                    **({"mime_type": stored.mime_type, "file_size": stored.size} if stored else {}),
                }
            }
        if kind == "document":
            return {
                "document": {
                    "file_id": file_id,
                    "file_unique_id": unique_id,
                    "file_name": stored.file_name if stored else "sandbox-file.bin",
                    "mime_type": stored.mime_type if stored else "application/octet-stream",
                    **({"file_size": stored.size} if stored else {}),
                }
            }
        if kind in ("audio", "voice"):
            return {
                kind: {
                    "file_id": file_id,
                    "file_unique_id": unique_id,
                    "duration": stored.duration if stored else 3,
                    **({"mime_type": stored.mime_type, "file_size": stored.size} if stored else {}),
                }
            }
        return {}  # pragma: no cover - guarded by `_FILE_MEDIA_KINDS`


@dataclass(slots=True)
class SandboxScenario:
    """A named span of activity: a tester opens one by hand before a manual
    check, and `qa/`'s e2e suite opens one per test. Every message and every
    Bot API call recorded while it is `SandboxStore.active_scenario_id` gets
    tagged with `id` (see `SandboxMessage.scenario_id`/`SandboxStore.record_api_call`),
    which is the whole point: a long sandbox run is otherwise one
    undifferentiated stream, and nobody reading it back afterwards can tell
    which test — or which manual click-through — produced which message.
    """

    #: Caller-supplied, or minted by `SandboxStore.next_scenario_id` when
    #: omitted. Also the dict key in `SandboxStore.scenarios`, so it is the
    #: one thing that can never change after creation.
    id: str
    name: str
    description: str | None = None
    #: Free-form provenance — "e2e", "manual", "preset" — *who* opened this,
    #: not what it was checking. `metadata` is where the "what" goes.
    source: str | None = None
    #: Which `FeatureSpec` this scenario was exercising, if the caller said.
    #: The axis a validator actually reads a run along: not "which of these
    #: 200 scenarios failed" but "is the captcha still correct", which is a
    #: question about a feature and only answerable by looking at every
    #: scenario that touched it at once. Left `None`, `control_api` still
    #: infers one from `tags` (`SandboxConfig.feature_for_tags`), so a suite
    #: that already labels its runs gets the grouping without changing.
    feature: str | None = None
    tags: list[str] = field(default_factory=list)
    #: Free-form: a test's nodeid and file, a `group_id`, the expectations it
    #: was asserting — whatever the caller wants attached. `PATCH .../{id}`
    #: merges into this key-by-key rather than replacing it outright, so a
    #: teardown step can add `outcome` without clobbering what setup recorded.
    metadata: dict[str, Any] = field(default_factory=dict)
    #: "running" | "passed" | "failed" | "skipped" | "closed" — a plain `str`,
    #: not a `Literal`, because the e2e suite and a human doing manual UAT are
    #: free to use whatever vocabulary tells the next reader what happened;
    #: this file does not gate behaviour on the value.
    status: str = "running"
    #: Timestamped breadcrumbs dropped mid-run — `{"at": float, "text": str,
    #: "level": "info"|"warn"|"error"}` — the thing that turns a scenario from
    #: a label into an actual account of what it saw happen.
    notes: list[dict[str, Any]] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None


@dataclass(slots=True)
class SandboxEvent:
    """What the web client is told happened. Streamed over SSE."""

    kind: str  # message | edit | delete | member | callback_answer | api_call | reset
    payload: dict[str, Any]
    at: float = field(default_factory=time.time)


class _PersistingUsers(dict[int, SandboxUser]):
    """`sandbox.users[id] = user` is how every caller — `control_api.py`'s
    `create_user`, both seed scenarios, `telegram_api.py`'s `_ensure_bot_user`
    — creates a user. Overriding just `__setitem__` catches every one of them
    without those modules needing to know persistence exists."""

    def __init__(self, store: SandboxStore) -> None:
        super().__init__()
        self._store = store

    def __setitem__(self, key: int, value: SandboxUser) -> None:
        super().__setitem__(key, value)
        self._store.db.save_user(value)


class _MemberRegistry(dict[int, Membership]):
    """A chat's `members` dict, upgraded in place by `_PersistingChats` so
    `chat.members[user_id] = Membership(...)` (`join_chat`, both seed
    scenarios) and `chat.members.setdefault(user_id, Membership(...))`
    (`restrictChatMember`/`banChatMember`/`promoteChatMember`) both persist."""

    def __init__(self, store: SandboxStore, chat_id: int) -> None:
        super().__init__()
        self._store = store
        self._chat_id = chat_id

    def __setitem__(self, key: int, value: Membership) -> None:
        super().__setitem__(key, value)
        self._store.db.save_member(self._chat_id, value)

    # Narrower than dict's `default: _VT | None = ...` overload: dict.setdefault's
    # C implementation never calls a subclass's __setitem__, so this has to be
    # overridden directly, and every caller here always passes a concrete
    # `Membership`, never None.
    def setdefault(self, key: int, default: Membership) -> Membership:
        if key in self:
            return self[key]
        self[key] = default
        return default


class _PersistingChats(dict[int, SandboxChat]):
    """`sandbox.chats[id] = chat` — `create_chat` and both seed scenarios."""

    def __init__(self, store: SandboxStore) -> None:
        super().__init__()
        self._store = store

    def __setitem__(self, key: int, value: SandboxChat) -> None:
        if not isinstance(value.members, _MemberRegistry):
            preexisting = dict(value.members)
            registry = _MemberRegistry(self._store, value.id)
            for user_id, membership in preexisting.items():
                registry[user_id] = membership
            value.members = registry
        super().__setitem__(key, value)
        self._store.db.save_chat(value)


class _PersistingScenarios(dict[str, SandboxScenario]):
    """`sandbox.scenarios[id] = scenario` — `control_api.py`'s `POST /scenarios`
    is the only creator. Mutations after creation (a note, a status change, a
    metadata merge) happen in place on the dataclass already in this dict and
    are caught by `_resync_mutable` instead, the same split every other
    persisting collection in this file uses."""

    def __init__(self, store: SandboxStore) -> None:
        super().__init__()
        self._store = store

    def __setitem__(self, key: str, value: SandboxScenario) -> None:
        super().__setitem__(key, value)
        self._store.db.save_scenario(value)


class SandboxStore:
    """The whole world. One instance per process; not thread-safe by design.

    In-memory dicts are the only read path. `db` is the write-through durable
    copy: `_PersistingUsers`/`_PersistingChats`/`_MemberRegistry` catch every
    dict write that creates a user, chat or membership; `_resync_mutable`
    catches the in-place attribute mutations (`membership.role = ...`,
    `message.edited = True`, ...) that no dict write causes, by re-saving
    everything mutable whenever `publish()` fires — which every one of those
    call sites already does, so no caller needs to change.
    """

    def __init__(self, db_path: str | None = None) -> None:
        self.db = SandboxDB(db_path if db_path is not None else get_config().db_path)
        self.users: dict[int, SandboxUser] = _PersistingUsers(self)
        self.chats: dict[int, SandboxChat] = _PersistingChats(self)
        self.messages: dict[int, list[SandboxMessage]] = {}
        #: Real bytes behind every photo, sticker and document in this run —
        #: see `files.py`. World state, not protocol state: it is cleared by
        #: `reset()` and restored on load, because a run whose pictures did not
        #: survive a restart is a run whose image features cannot be reviewed.
        self.files = FileStore()
        self.scenarios: dict[str, SandboxScenario] = _PersistingScenarios(self)
        #: Which scenario, if any, new messages and API calls get stamped
        #: with — see `add_message`/`record_api_call`. Session state, like
        #: `pending_updates`: not persisted, not restored across a restart, and
        #: cleared by `reset()`. A scenario that was running when the process
        #: died is not silently still running when it comes back; the tester
        #: (or the e2e teardown that never got to run) has to say so again.
        self.active_scenario_id: str | None = None
        #: Updates the gateway has not collected yet, in Telegram's own shape.
        #: Protocol state, not world state — never persisted, never restored.
        self.pending_updates: list[dict[str, Any]] = []
        #: Every Bot API call the bot made, newest last — the UI's "what did the
        #: bot actually do" panel, which is most of this tool's validation value.
        self.api_calls: list[dict[str, Any]] = []
        self.events: list[SandboxEvent] = []
        self._subscribers: list[asyncio.Queue[SandboxEvent]] = []
        self._update_ids = itertools.count(1)
        self._message_ids = itertools.count(1000)
        self._user_ids = itertools.count(500_000_001)
        self._chat_ids = itertools.count(-1_001_000_000_001, -1)
        #: Unlike `_update_ids`/`_message_ids`, nothing outside this process
        #: ever remembers a scenario id, so there is nothing for a reused one
        #: to collide with — it resets to its base value on `reset()` exactly
        #: like `_user_ids`/`_chat_ids` just above.
        self._scenario_ids = itertools.count(1)
        #: Callback query ids `control_api.py` has queued as updates but the
        #: bot has not yet answered — see `queue_update`/`consume_callback_query`.
        #: Purely protocol bookkeeping (mirrors `pending_updates`): never
        #: persisted, cleared by `reset()`.
        self._pending_callback_queries: set[str] = set()
        #: True while a `getUpdates` long-poll is in flight. Real Telegram
        #: rejects a second concurrent `getUpdates` for the same bot with 409
        #: Conflict rather than queueing it — the exact shape of "two replicas
        #: somehow polling the same token", which is worth catching here
        #: rather than silently letting both succeed.
        self._updates_polling = False
        #: `getUpdates`' `allowed_updates`: "if not specified, the previous
        #: setting will be used" (Bot API docs) — so a filter set on one call
        #: must survive into the next call that omits the parameter.
        self.allowed_updates: list[str] | None = None
        #: `setMyCommands`/`getMyCommands`/`deleteMyCommands`, keyed by
        #: `(scope, language_code)` exactly as real Telegram scopes them.
        #: Bot configuration, not world state a scenario builds — reset like
        #: `pending_updates`, not persisted like a chat or a message.
        self.bot_commands: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.load()

    # ---------------------------------------------------------------- ids

    def next_update_id(self) -> int:
        """Monotonic across the whole process, and now across a `reset()` or
        even a restart too — this one counter is the single most confusing
        failure mode this tool can produce if it ever goes backwards.

        A production bot almost always dedupes updates by `update_id` in a
        store that outlives this process — a Redis/Valkey key with a TTL is
        the usual shape — and it runs that middleware here exactly as it
        would against real Telegram. That store has no idea the sandbox was
        reset: if `update_id` ever restarted at 1, the first updates after a
        reset would reuse ids it already has recorded (within the TTL) as
        delivered, so it would silently treat them as redeliveries and drop
        them before any handler ran. From the outside the bot simply looks
        dead, with nothing in this file's own logs to explain why — the
        single most confusing failure this tool can produce. Persisting the
        high-water mark here (and resuming past it in `_resync_counters`) is
        what makes "press Reset constantly" safe advice instead of a trap.
        """
        value = next(self._update_ids)
        self.db.save_counter("update_id_high_water", value)
        return value

    def next_message_id(self) -> int:
        """Same reasoning as `next_update_id`, one layer down: `reset()`
        reseeds at the *same* chat id every time (the chat counter does
        restart), so without this a message minted right after a reset could
        reuse the exact `(chat_id, message_id)` pair a message from before
        the reset used — and anything in the bot's own database that still
        remembers that pair (moderation history, media references) would be
        looking at the wrong message."""
        value = next(self._message_ids)
        self.db.save_counter("message_id_high_water", value)
        return value

    def next_user_id(self) -> int:
        return next(self._user_ids)

    def next_chat_id(self) -> int:
        return next(self._chat_ids)

    def next_scenario_id(self) -> str:
        """Only used when a caller omits `id` on `POST /scenarios` — one whose
        own id already tells them what to call it (a test's nodeid, say) never
        needs this."""
        return f"scenario-{next(self._scenario_ids)}"

    # ------------------------------------------------------- getUpdates lock

    def begin_polling(self) -> bool:
        """True if this call claimed the single `getUpdates` slot; False if
        another poll is already in flight, which is real Telegram's 409
        Conflict."""
        if self._updates_polling:
            return False
        self._updates_polling = True
        return True

    def end_polling(self) -> None:
        self._updates_polling = False

    # -------------------------------------------------------- callback queries

    def register_callback_query(self, query_id: str) -> None:
        """Record a callback query id as issued-but-unanswered.

        `queue_update` does this for every update it queues, which covers the
        web client and anything driving `/api/...`. A harness that feeds
        updates straight into the bot's dispatcher — skipping the control
        plane and the poll loop entirely — has to say so itself, or the bot's
        perfectly correct `answerCallbackQuery` comes back "query is too old
        or invalid" for an id this store never saw issued. That failure reads
        as a broken handler and is nothing of the sort.
        """
        self._pending_callback_queries.add(query_id)

    def consume_callback_query(self, query_id: str) -> bool:
        """True the first time this id is answered; False for an id that was
        never issued or has already been answered — real Telegram's "query is
        too old / invalid" case for `answerCallbackQuery`."""
        if query_id in self._pending_callback_queries:
            self._pending_callback_queries.discard(query_id)
            return True
        return False

    # ------------------------------------------------------------ lookups

    def message(self, chat_id: int, message_id: int) -> SandboxMessage | None:
        for candidate in self.messages.get(chat_id, ()):
            if candidate.message_id == message_id:
                return candidate
        return None

    def membership(self, chat_id: int, user_id: int) -> Membership | None:
        chat = self.chats.get(chat_id)
        return chat.members.get(user_id) if chat else None

    def is_admin(self, chat_id: int, user_id: int) -> bool:
        member = self.membership(chat_id, user_id)
        return member is not None and member.role in ("creator", "administrator")

    def chat_by_username(self, username: str) -> SandboxChat | None:
        """Resolves the `chat_id: "@name"` form real Telegram accepts
        alongside a numeric id — a plain linear scan, because the sandbox has
        at most a handful of chats and a second index would be more code than
        the lookup it replaces."""
        target = username.lstrip("@").lower()
        for chat in self.chats.values():
            if chat.username is not None and chat.username.lower() == target:
                return chat
        return None

    # ------------------------------------------------------------- writes

    def add_message(self, message: SandboxMessage) -> SandboxMessage:
        # Stamped here, once, regardless of what the caller set — this is the
        # single choke point every message-creating call site in
        # `control_api.py`/`telegram_api.py` goes through, which is what makes
        # "tagged with whatever scenario was running at the moment it
        # happened" true without either module needing to know a scenario
        # concept exists.
        message.scenario_id = self.active_scenario_id
        self.messages.setdefault(message.chat_id, []).append(message)
        self.db.save_message(message)
        return message

    def store_file(
        self,
        data: bytes,
        *,
        file_name: str = "",
        declared_mime: str | None = None,
        duration: int = 0,
    ) -> SandboxFile:
        """Add bytes to the file store and mirror them to disk.

        The one write path for media, used by both API surfaces — the control
        plane when a tester attaches a picture, and the Telegram surface when
        the *bot* uploads one. Going through here rather than `self.files.add`
        directly is what keeps a run's images in the DuckDB file, so reopening
        it later shows the pictures rather than a wall of grey boxes.
        """
        stored = self.files.add(
            data, file_name=file_name, declared_mime=declared_mime, duration=duration
        )
        self.db.save_file(stored)
        return stored

    def queue_update(self, update: dict[str, Any]) -> dict[str, Any]:
        """Hand an update to the gateway's next `getUpdates` poll.

        `control_api.py` is the only caller, and it never imports this
        module's private state — noticing a `callback_query` update here
        (rather than requiring a second call from `control_api.py`) is what
        lets `answerCallbackQuery` reject an unknown/already-answered id
        without the control plane needing to know that tracking exists.
        """
        update["update_id"] = self.next_update_id()
        self.pending_updates.append(update)
        callback_query = update.get("callback_query")
        if isinstance(callback_query, dict):
            query_id = callback_query.get("id")
            if isinstance(query_id, str):
                self._pending_callback_queries.add(query_id)
        return update

    @staticmethod
    def _update_type(update: dict[str, Any]) -> str:
        """The one key besides `update_id` — Telegram's own `allowed_updates`
        vocabulary names updates by exactly this key (`"message"`,
        `"callback_query"`, ...)."""
        for key in update:
            if key != "update_id":
                return key
        return ""  # pragma: no cover - queue_update always sets exactly one

    def take_updates(
        self,
        offset: int | None = None,
        limit: int = 100,
        allowed_updates: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Telegram's confirm-by-offset contract: an update stays queued until the
        client asks for one past it, which is what makes redelivery real here.

        A negative `offset` is its own documented case (`getUpdates` docs):
        "retrieve updates starting from -offset update from the end of the
        updates queue. All previous updates will be forgotten" — distinct from
        a non-negative offset, which confirms everything strictly before it.

        `allowed_updates`, when given, only narrows what is *returned*; a
        filtered-out update is still confirmed (and can still be redelivered)
        exactly like one the caller saw and didn't ask past — real Telegram's
        own queue does not special-case a type nobody asked for out of
        existence, it just never shows it to that particular caller.
        """
        if offset is not None and offset < 0:
            keep = min(-offset, len(self.pending_updates))
            self.pending_updates = self.pending_updates[-keep:] if keep else []
        elif offset is not None:
            self.pending_updates = [u for u in self.pending_updates if u["update_id"] >= offset]
        visible = self.pending_updates
        if allowed_updates is not None:
            allowed = set(allowed_updates)
            visible = [u for u in visible if self._update_type(u) in allowed]
        return visible[:limit]

    def record_api_call(self, method: str, payload: dict[str, Any]) -> None:
        # Same stamping rule as `add_message`: whatever scenario is active
        # *right now*, never revisited later even if that scenario ends or a
        # new one starts before anyone reads this call back.
        at = time.time()
        scenario_id = self.active_scenario_id
        self.api_calls.append(
            {"method": method, "payload": payload, "at": at, "scenario_id": scenario_id}
        )
        self.db.save_api_call(method, payload, at, scenario_id)

    # -------------------------------------------------------------- events

    def publish(self, kind: str, payload: dict[str, Any]) -> None:
        # Every membership/message mutation that isn't itself a dict write —
        # `membership.role = ...`, `message.edited = True`, and the like —
        # happens right before its call site publishes an event. Re-saving
        # everything mutable here is cheap at sandbox scale and catches those
        # without `control_api.py`/`telegram_api.py` needing a persistence call.
        if kind != "reset":
            self._resync_mutable()
        event = SandboxEvent(kind=kind, payload=payload)
        self.events.append(event)
        for queue in list(self._subscribers):
            queue.put_nowait(event)

    def subscribe(self) -> asyncio.Queue[SandboxEvent]:
        queue: asyncio.Queue[SandboxEvent] = asyncio.Queue(maxsize=1000)
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[SandboxEvent]) -> None:
        if queue in self._subscribers:
            self._subscribers.remove(queue)

    # ---------------------------------------------------------- persistence

    def _resync_mutable(self) -> None:
        """Re-save every chat, membership and message as they currently stand.

        Creation is caught by the persisting dicts above; this is for the
        mutations those cannot see — a `SandboxChat`/`Membership`/`SandboxMessage`/
        `SandboxScenario` field set directly on an object already in the dicts,
        which is how `patch_member`, `restrictChatMember`/`banChatMember`/
        `promoteChatMember`, `setChatTitle`/`setChatDescription`/
        `setChatPermissions`/`pinChatMessage`, `editMessageText`,
        `deleteMessage`, and every scenario route past creation (a note, a
        status change, `PATCH .../{id}`'s metadata merge) all work. Idempotent
        and cheap: this is a scenario workbench with dozens of rows, not
        thousands.
        """
        for chat in self.chats.values():
            self.db.save_chat(chat)
            for membership in chat.members.values():
                self.db.save_member(chat.id, membership)
        for chat_messages in self.messages.values():
            for message in chat_messages:
                self.db.save_message(message)
        for scenario in self.scenarios.values():
            self.db.save_scenario(scenario)

    def load(self) -> None:
        """Restore the durable copy into memory. Called once, from `__init__`
        — a fresh `SandboxStore` always starts from whatever was last saved."""
        self.db.load_into(self)
        self._resync_counters()

    def _resync_counters(self) -> None:
        """A restored id must not collide with one this run mints next, so
        each counter resumes one step past the most extreme id on disk.

        `update_id` has no rows to scan — `pending_updates` is protocol state,
        never persisted (see `__init__`) — so `sandbox_counters`' high-water
        mark is its *only* source of truth after a restart. `message_id` has
        both a row-based signal (the messages actually on disk) and the
        counter; taking the max of the two covers the same case `reset()`
        exercises within a single process, where the counter has advanced but
        `sandbox_messages` was just cleared.
        """
        counters = self.db.load_counters()
        if self.users:
            self._user_ids = itertools.count(max(500_000_001, max(self.users) + 1))
        if self.chats:
            # Chat ids count downward from a large negative starting point.
            self._chat_ids = itertools.count(min(-1_001_000_000_001, min(self.chats) - 1), -1)
        message_ids = [
            m.message_id for chat_messages in self.messages.values() for m in chat_messages
        ]
        self._message_ids = itertools.count(
            max(
                1000,
                counters.get("message_id_high_water", 0) + 1,
                *(mid + 1 for mid in message_ids),
            )
        )
        self._update_ids = itertools.count(max(1, counters.get("update_id_high_water", 0) + 1))
        # No persisted high-water mark for this one — nothing outside this
        # process ever remembers a scenario id the way Valkey remembers an
        # update id, so the restored rows are the only signal there is. But a
        # signal there is: without this, a restart followed by `POST
        # /scenarios` with no `id` would re-mint `scenario-1` forever,
        # 409-ing against the one just restored from disk on every retry.
        scenario_suffixes = [
            int(scenario_id.removeprefix("scenario-"))
            for scenario_id in self.scenarios
            if scenario_id.startswith("scenario-")
            and scenario_id.removeprefix("scenario-").isdigit()
        ]
        if scenario_suffixes:
            self._scenario_ids = itertools.count(max(scenario_suffixes) + 1)

    def close(self) -> None:
        """Release the DuckDB write lock — mainly for tests that open a
        second `SandboxStore` on the same file and need the first out of the
        way first."""
        self.db.close()

    # --------------------------------------------------------------- reset

    def reset(self) -> None:
        """Wipe everything except live subscribers — the UI stays connected.

        `_update_ids` and `_message_ids` are deliberately *not* reset to their
        base values here, unlike `_user_ids`/`_chat_ids` just below — see
        `next_update_id`'s docstring. `sandbox_counters` is the one table
        `self.db.clear()` does not touch, so the persisted high-water mark
        those two counters already carry survives this call unchanged; there
        is nothing left to do but leave the in-memory `itertools.count`
        objects exactly where they are.
        """
        self.users.clear()
        self.chats.clear()
        self.messages.clear()
        self.files.clear()
        self.scenarios.clear()
        self.active_scenario_id = None
        self.pending_updates.clear()
        self.api_calls.clear()
        self.events.clear()
        self._pending_callback_queries.clear()
        self.allowed_updates = None
        self.bot_commands.clear()
        self.db.clear()
        self._user_ids = itertools.count(500_000_001)
        self._chat_ids = itertools.count(-1_001_000_000_001, -1)
        self._scenario_ids = itertools.count(1)
        self.publish("reset", {})


_store = SandboxStore()


def store() -> SandboxStore:
    return _store
