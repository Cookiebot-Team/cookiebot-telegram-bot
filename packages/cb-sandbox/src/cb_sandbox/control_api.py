"""The control plane the web client and the test kit drive.

Every action here is a human clicking something in a browser (or a test doing
the same over HTTP) — switch user, join a group, send a message, press a
button, toggle admin/anonymous — and each one becomes a real Telegram update
on `SandboxStore.pending_updates` via `store().queue_update(...)`. The bot
long-polls `getUpdates` from `telegram_api.py` exactly as it would poll
Telegram, so what's exercised here is the production handler stack, not a
re-implementation of it.

Nothing in this module knows which bot it is serving. Identity, seed fixtures,
features, commands and presets all come from `cb_sandbox.config` — see
`GET /api/kit`, which hands the client every one of those so the same client
works against any bot's sandbox.

Three routes carry most of the validation value:

    GET /api/state      the world, plus every scenario and the feature rollup
    GET /api/features   one row per feature: which scenarios ran, how they ended
    GET /api/kit        what this bot *is* — identity, seeds, features, commands

`state.py` owns the world (`SandboxStore`); this module only reads and writes
through its public API and translates to/from the JSON shapes `web/types.ts`
declares. Response bodies for `GET /api/state` must match that file
field-for-field — see `_snapshot` below.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import itertools
import json
import time
from collections.abc import AsyncIterator
from typing import Any, Literal, cast

from cb_sandbox.config import (
    FeatureSpec,
    SandboxConfig,
    SeedFixture,
    get_config,
)
from cb_sandbox.files import SandboxFile
from cb_sandbox.logging import get_logger
from cb_sandbox.state import (
    ANONYMOUS_BOT_ID,
    ChatType,
    Membership,
    Role,
    SandboxChat,
    SandboxMessage,
    SandboxScenario,
    SandboxStore,
    SandboxUser,
    store,
)
from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

log = get_logger("cb.sandbox.control")

router = APIRouter(prefix="/api")


def bot_id() -> int:
    """The configured bot's id — see `telegram_api.bot_id` for why this is a
    call and not a module constant."""
    return get_config().bot.id


#: Every media kind a stored `SandboxMessage` can carry — the four a human can
#: attach in the client plus the ones only the *bot* produces, via `sendDocument`
#: / `sendAudio` / `sendVoice` / `sendDice`. Both halves have to be here: this
#: union types `MessageOut.media`, which every message in `GET /api/state` is
#: validated against, so leaving the bot-only kinds out turns the bot sending a
#: document into a 500 on the whole snapshot rather than one odd-looking bubble.
MediaKind = Literal["photo", "sticker", "video", "animation", "document", "audio", "voice", "dice"]
#: What the *client* may attach. `dice` is excluded deliberately: real Telegram
#: mints the roll server-side (`sendDice` fills `media_extra`), so a user-sent
#: one would be a die with no value.
SendMediaKind = Literal["photo", "sticker", "video", "animation", "document", "audio", "voice"]

#: Callback query ids are Telegram's own opaque token, distinct from the
#: update-id sequence `SandboxStore` hands out — a separate counter here keeps
#: the two from ever being confused with each other.
_callback_ids = itertools.count(1)

# --------------------------------------------------------------------- requests


class CreateUserRequest(BaseModel):
    first_name: str = Field(min_length=1)
    username: str = Field(min_length=1)
    language_code: str = "en"


class CreateChatRequest(BaseModel):
    title: str = Field(min_length=1)
    #: Defaults to a group, matching `SandboxChat`'s own default — but a
    #: tester needs to be able to open a *private* chat on demand, not only
    #: through the fixed `dm` seed scenario's one hardcoded account, to drive
    #: any command gated on `message.chat.type == "private"`. This chat's id is
    #: minted from the same counter every chat gets, so it is *not* a DM the bot
    #: can reach — a handler answers privately by the recipient's own user id.
    #: Use `POST /users/{user_id}/dm` for that.
    type: ChatType = "supergroup"


class JoinRequest(BaseModel):
    user_id: int
    #: Who added them. Omitted means the user joined themselves — the
    #: self-join vs. added-by-another fork that join-time checks (a
    #: blocklist, a captcha, a welcome) almost always branch on.
    by_user_id: int | None = None


class LeaveRequest(BaseModel):
    user_id: int
    #: Who removed them. Omitted means they left on their own (Telegram's
    #: "left" status); present means they were kicked (Telegram's "kicked"
    #: status), with `from` on the service message set to the remover.
    by_user_id: int | None = None


class PatchMemberRequest(BaseModel):
    role: Role | None = None
    anonymous: bool | None = None


class SendMessageRequest(BaseModel):
    user_id: int
    text: str | None = None
    reply_to_message_id: int | None = None
    media: SendMediaKind | None = None
    #: A `file_id` from `POST /api/files` — the actual bytes behind the media.
    #: Optional: a send with `media` and no file still works and renders as a
    #: labelled placeholder, which is what a flood test wants (six stickers,
    #: nobody cares what they look like). Attaching a real picture is for the
    #: other kind of check: does the bot read this image correctly.
    media_file_id: str | None = None
    media_caption: str | None = None
    #: Requires the sender's membership to already have `anonymous` toggled
    #: on (see `PatchMemberRequest`) — sending anonymously is not itself what
    #: turns anonymity on, exactly as Telegram models it.
    anonymous: bool = False


class UploadFileRequest(BaseModel):
    """Bytes for a picture, sticker or document, as the browser can send them.

    Base64 rather than multipart because the web client already has the data as
    a data URL from `FileReader`, and a JSON body keeps this route the same
    shape as every other one in this file. `data` accepts either a bare base64
    payload or a whole `data:image/png;base64,...` URL.
    """

    filename: str = ""
    #: Only a fallback: the server sniffs the real type from the bytes, because
    #: what a handler sees is what the bytes say, not what the uploader claimed.
    content_type: str | None = None
    data: str = Field(min_length=1)
    #: For audio/video, which carry one on the wire and which nothing here can
    #: derive without decoding the container.
    duration: int = 0


class CallbackRequest(BaseModel):
    user_id: int
    message_id: int
    data: str


class SeedRequest(BaseModel):
    #: A seed fixture's name, from `sandbox.config.json` (or the built-in
    #: three). A plain `str`, not a `Literal`: which worlds exist is a fact
    #: about the bot being tested, not about this file, and an unknown name
    #: 400s with the list of real ones — which is a better error than a 422
    #: naming a union the config may have replaced entirely.
    scenario: str | None = None


#: `level` on a scenario note — deliberately not reused for `SandboxScenario.status`,
#: which is a plain `str` (see that dataclass's docstring): a note's severity is
#: a small closed vocabulary the client renders differently, a scenario's
#: outcome is whatever the caller (a test runner, a human) wants to call it.
NoteLevel = Literal["info", "warn", "error"]


class CreateScenarioRequest(BaseModel):
    #: Omitted means minted from `SandboxStore.next_scenario_id` — present
    #: means the caller already has a natural name for it (a test's nodeid),
    #: which is the common case once `qa/`'s e2e suite is driving this.
    id: str | None = None
    name: str = Field(min_length=1)
    description: str | None = None
    source: str | None = None
    #: Which `FeatureSpec` this scenario is checking. Optional, and worth
    #: setting: it is the axis validation actually happens along — "is the
    #: captcha still correct" is a question about one feature and every
    #: scenario that touched it, not about one scenario. A caller that leaves
    #: it out still gets grouped if its `tags` name a configured feature.
    feature: str | None = None
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    #: True by default: opening a scenario and then immediately activating it
    #: is what every caller wants, and the two-step alternative (create, then
    #: a second call to `.../activate`) exists only for the rarer case of
    #: preparing a scenario before it should start capturing anything.
    activate: bool = True


class ScenarioNoteRequest(BaseModel):
    text: str = Field(min_length=1)
    level: NoteLevel = "info"


class ScenarioPatchRequest(BaseModel):
    """Every field optional and independently settable — a teardown step
    setting `status` should not have to re-send `tags` just to leave them
    alone. `metadata` merges key-by-key (`dict.update`); `tags`, having no
    natural per-key identity, replaces outright."""

    status: str | None = None
    description: str | None = None
    feature: str | None = None
    tags: list[str] | None = None
    metadata: dict[str, Any] | None = None


class EndScenarioRequest(BaseModel):
    status: str | None = None


#: `POST .../end` needs no body at all in the common case ("just close it,
#: whatever it did"), but a `BaseModel`-typed parameter with no default makes
#: FastAPI treat the request body as required and 422 a bodyless call. A
#: module-level singleton default (never mutated — every field is read-only
#: here) is what ruff's B008 wants in place of calling the model inline.
_DEFAULT_END_SCENARIO_REQUEST = EndScenarioRequest()


# --------------------------------------------------------------------- responses
#
# These mirror `web/types.ts` field-for-field. Do not add or rename a field
# without updating that file too — it is not owned by this module.


class UserOut(BaseModel):
    id: int
    first_name: str
    last_name: str | None = None
    username: str
    language_code: str
    is_bot: bool


class MembershipOut(BaseModel):
    user_id: int
    role: Role
    anonymous: bool
    joined_at: float
    restricted_until: float


class ChatOut(BaseModel):
    id: int
    title: str
    type: ChatType
    members: list[MembershipOut]


class MessageOut(BaseModel):
    message_id: int
    chat_id: int
    from_id: int
    text: str | None
    date: float
    sender_chat_id: int | None
    reply_to_message_id: int | None
    reply_markup: dict[str, Any] | None
    media: MediaKind | None
    #: Which stored file this message's media is, if any — the client fetches
    #: `GET /api/files/{id}` to render it. `null` means the media has no bytes
    #: here (a `file_id` minted by production, a fixture), which the client
    #: shows as a labelled placeholder rather than a broken image.
    media_file_id: str | None
    media_caption: str | None
    #: `text`/`media_caption` are the plain strings real Telegram stores, with
    #: the `parse_mode` markup already parsed out (`telegram_api._apply_parse_mode`).
    #: Without these two lists the client has no way to render the bold, the
    #: links or the code blocks the bot actually sent — it would show the text
    #: stripped of every formatting the handler asked for, which is precisely
    #: the kind of silent difference a UAT session exists to catch.
    entities: list[dict[str, Any]]
    caption_entities: list[dict[str, Any]]
    #: `{"kind": "join"|"leave", "user_id": N, "by_user_id": N|null}` on a
    #: membership service message, `null` on an ordinary one. The client needs
    #: the discriminator to render "Bob joined" rather than an empty bubble —
    #: these messages carry no text by design.
    service: dict[str, Any] | None
    edited: bool
    deleted: bool
    #: Which `SandboxScenario` was running when this message was recorded —
    #: `None` for most traffic, until a caller opens one. See
    #: `SandboxMessage.scenario_id`'s docstring for why this is stamped once
    #: and never revisited.
    scenario_id: str | None


class FileOut(BaseModel):
    """A stored file, as the client needs to describe and fetch it. The bytes
    themselves come from `GET /api/files/{file_id}`; this is metadata only, so
    a snapshot never carries megabytes of base64."""

    file_id: str
    file_unique_id: str
    mime_type: str
    file_name: str
    size: int
    width: int
    height: int
    duration: int


class ApiCallOut(BaseModel):
    method: str
    payload: dict[str, Any]
    at: float
    #: Same stamping rule as `MessageOut.scenario_id`, applied to the Bot API
    #: call log instead of the chat transcript — together the two are what let
    #: a human filter a long run down to one test's traffic.
    scenario_id: str | None


class ScenarioOut(BaseModel):
    id: str
    name: str
    description: str | None = None
    source: str | None = None
    #: The feature this scenario was checking — either what the caller set, or
    #: what `SandboxConfig.feature_for_tags` inferred from its tags. Resolved
    #: on read rather than frozen at creation so that adding a feature to the
    #: config retroactively groups the scenarios that already match it,
    #: instead of only the ones recorded afterwards.
    feature: str | None = None
    tags: list[str]
    metadata: dict[str, Any]
    status: str
    notes: list[dict[str, Any]]
    started_at: float
    ended_at: float | None = None
    #: Computed, not stored — how many currently-stored messages/API calls
    #: carry this scenario's id, counted fresh on every read rather than kept
    #: as a running tally that could drift from a `reset()` or a message that
    #: gets marked `deleted` after the fact.
    message_count: int
    api_call_count: int


class FeatureOut(BaseModel):
    """One configured feature, plus what this run has to say about it.

    The counts are the point. A feature's own metadata (title, status, which
    commands trigger it) is static and could be read straight from the config
    file; what a validator needs is that metadata *next to* "4 scenarios ran,
    3 passed, 1 failed" — which requires the config and the run in one place,
    and this is that place.
    """

    id: str
    title: str
    description: str | None = None
    status: str
    commands: list[str]
    tags: list[str]
    docs: str | None = None
    #: Ids of the scenarios claimed by this feature, oldest first — the exact
    #: set the web client filters the timeline and the API-call log down to.
    scenario_ids: list[str]
    scenario_count: int
    #: `{status: count}` over those scenarios, using whatever vocabulary the
    #: callers used ("passed"/"failed"/"running"/...). Not normalised to a
    #: fixed set: a suite that reports "flaky" should see "flaky", not have it
    #: silently folded into something this file made up.
    status_counts: dict[str, int]
    message_count: int
    api_call_count: int


class SeedOut(BaseModel):
    """A seed fixture as the web client's picker needs it — the name to POST
    back, and enough prose to know what pressing it gets you."""

    name: str
    title: str
    description: str
    user_count: int
    chat_count: int


class CommandOut(BaseModel):
    primary: str
    canonical: str
    aliases: list[str]
    feature_id: str | None = None
    title: str | None = None
    status: str
    hint: str | None = None


class PresetOut(BaseModel):
    id: str
    button: str
    label: str
    seed: str
    feature_id: str | None = None
    acting_user: str | None = None
    create_user: dict[str, Any] | None = None
    chat: str | None = None
    what_to_do: str
    what_to_look_for: str


class BotOut(BaseModel):
    id: int
    username: str
    first_name: str


class KitOut(BaseModel):
    """Everything about *this* bot that the web client would otherwise have to
    hardcode: identity, seeds, presets, commands, features.

    Served rather than compiled in, so the same client binary works against
    any bot's sandbox — swapping `sandbox.config.json` and restarting the
    server is the entire integration.
    """

    bot: BotOut
    config_source: str
    default_seed: str
    seeds: list[SeedOut]
    presets: list[PresetOut]
    commands: list[CommandOut]
    features: list[FeatureOut]


class SandboxSnapshot(BaseModel):
    users: list[UserOut]
    chats: list[ChatOut]
    messages: dict[int, list[MessageOut]]
    api_calls: list[ApiCallOut]
    bot: UserOut | None
    #: Ordered by `started_at`, oldest first — the order a dropdown in the web
    #: client would want to list them in.
    scenarios: list[ScenarioOut]
    active_scenario_id: str | None
    #: Every configured feature with this run's counts folded in. Carried on
    #: the snapshot rather than left to a second request so the client can
    #: never render a feature rollup one poll out of step with the scenario
    #: list it is summarising.
    features: list[FeatureOut]


# --------------------------------------------------------------------- conversions


def _user_out(user: SandboxUser) -> UserOut:
    return UserOut.model_validate(user, from_attributes=True)


def _membership_out(membership: Membership) -> MembershipOut:
    return MembershipOut.model_validate(membership, from_attributes=True)


def _chat_out(chat: SandboxChat) -> ChatOut:
    members = sorted(
        (_membership_out(member) for member in chat.members.values()),
        key=lambda member: member.user_id,
    )
    return ChatOut(id=chat.id, title=chat.title, type=chat.type, members=members)


def _message_out(message: SandboxMessage) -> MessageOut:
    return MessageOut.model_validate(message, from_attributes=True)


def _scenario_counts(sandbox: SandboxStore, scenario_id: str) -> tuple[int, int]:
    """`message_count`/`api_call_count`: a plain scan, not a maintained
    counter — see `ScenarioOut.message_count`'s docstring for why."""
    message_count = sum(
        1
        for chat_messages in sandbox.messages.values()
        for message in chat_messages
        if message.scenario_id == scenario_id
    )
    api_call_count = sum(1 for call in sandbox.api_calls if call.get("scenario_id") == scenario_id)
    return message_count, api_call_count


def _scenario_feature(scenario: SandboxScenario, config: SandboxConfig) -> str | None:
    """What a scenario is filed under: what it declared, else what its tags
    imply. Explicit always wins — a caller that says `feature="captcha"` has
    stated something the tag heuristic must not second-guess."""
    if scenario.feature:
        return scenario.feature
    return config.feature_for_tags(scenario.tags)


def _scenario_out(sandbox: SandboxStore, scenario: SandboxScenario) -> ScenarioOut:
    message_count, api_call_count = _scenario_counts(sandbox, scenario.id)
    return ScenarioOut(
        id=scenario.id,
        name=scenario.name,
        description=scenario.description,
        source=scenario.source,
        feature=_scenario_feature(scenario, get_config()),
        tags=list(scenario.tags),
        metadata=dict(scenario.metadata),
        status=scenario.status,
        notes=list(scenario.notes),
        started_at=scenario.started_at,
        ended_at=scenario.ended_at,
        message_count=message_count,
        api_call_count=api_call_count,
    )


def _feature_out(spec: FeatureSpec, scenarios: list[ScenarioOut]) -> FeatureOut:
    """One feature, with this run's scenarios already resolved onto it.

    `scenarios` is the *already-computed* `ScenarioOut` list, not the raw
    store: their `feature` field has been resolved once (explicit or inferred)
    and re-deriving it here would be a second chance to disagree with what the
    client is being shown in the scenario list right next to this rollup.
    """
    mine = [scenario for scenario in scenarios if scenario.feature == spec.id]
    status_counts: dict[str, int] = {}
    for scenario in mine:
        status_counts[scenario.status] = status_counts.get(scenario.status, 0) + 1
    return FeatureOut(
        id=spec.id,
        title=spec.title,
        description=spec.description,
        status=spec.status,
        commands=list(spec.commands),
        tags=list(spec.tags),
        docs=spec.docs,
        scenario_ids=[scenario.id for scenario in mine],
        scenario_count=len(mine),
        status_counts=status_counts,
        message_count=sum(scenario.message_count for scenario in mine),
        api_call_count=sum(scenario.api_call_count for scenario in mine),
    )


def _features_out(config: SandboxConfig, scenarios: list[ScenarioOut]) -> list[FeatureOut]:
    return [_feature_out(spec, scenarios) for spec in config.features]


def _snapshot(sandbox: SandboxStore) -> SandboxSnapshot:
    config = get_config()
    self_id = config.bot.id
    bot = sandbox.users.get(self_id)
    # The bot and the synthetic GroupAnonymousBot are not accounts a human can
    # switch to and act as, so neither belongs in the switchable user roster.
    users = sorted(
        (_user_out(u) for u in sandbox.users.values() if u.id not in (self_id, ANONYMOUS_BOT_ID)),
        key=lambda u: u.id,
    )
    chats = sorted((_chat_out(c) for c in sandbox.chats.values()), key=lambda c: c.id)
    messages = {
        chat_id: [_message_out(message) for message in chat_messages]
        for chat_id, chat_messages in sandbox.messages.items()
    }
    api_calls = [ApiCallOut.model_validate(call) for call in sandbox.api_calls]
    scenarios = sorted(
        (_scenario_out(sandbox, s) for s in sandbox.scenarios.values()),
        key=lambda s: s.started_at,
    )
    return SandboxSnapshot(
        users=users,
        chats=chats,
        messages=messages,
        api_calls=api_calls,
        bot=_user_out(bot) if bot is not None else None,
        scenarios=scenarios,
        active_scenario_id=sandbox.active_scenario_id,
        features=_features_out(config, scenarios),
    )


# --------------------------------------------------------------------- lookups


def _get_chat(sandbox: SandboxStore, chat_id: int) -> SandboxChat:
    chat = sandbox.chats.get(chat_id)
    if chat is None:
        raise HTTPException(status_code=404, detail=f"chat {chat_id} not found")
    return chat


def _get_user(sandbox: SandboxStore, user_id: int) -> SandboxUser:
    user = sandbox.users.get(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail=f"user {user_id} not found")
    return user


def _get_scenario(sandbox: SandboxStore, scenario_id: str) -> SandboxScenario:
    scenario = sandbox.scenarios.get(scenario_id)
    if scenario is None:
        raise HTTPException(status_code=404, detail=f"scenario {scenario_id} not found")
    return scenario


def _ensure_anonymous_bot(sandbox: SandboxStore) -> SandboxUser:
    """Telegram's GroupAnonymousBot, materialised lazily so `from` on an
    anonymous message resolves to a real, well-formed user instead of the
    bare `{"id": ..., "is_bot": False}` fallback `SandboxMessage.as_telegram`
    uses for an unknown sender — that fallback is missing `first_name`, which
    aiogram's `User` model requires."""
    user = sandbox.users.get(ANONYMOUS_BOT_ID)
    if user is None:
        user = SandboxUser(
            id=ANONYMOUS_BOT_ID, first_name="Group", username="GroupAnonymousBot", is_bot=True
        )
        sandbox.users[user.id] = user
    return user


def _command_entities(text: str) -> list[dict[str, Any]]:
    """A leading `/token` needs a `bot_command` entity or aiogram's command
    filters never fire — the entity, not the leading slash, is what Telegram
    (and aiogram) actually key command dispatch on."""
    if not text.startswith("/"):
        return []
    token = text.split(maxsplit=1)[0]
    return [{"type": "bot_command", "offset": 0, "length": len(token)}]


# --------------------------------------------------------------------- scenarios


def _ensure_bot_user(sandbox: SandboxStore) -> SandboxUser:
    """The bot's own account, materialised from config.

    Several features in any moderation bot (restricting media, opening an
    admin menu, paging the admins) check "am I an administrator here" before
    acting, so a seed that leaves the bot out of its own group is not a
    smaller world — it is a different one, where half the commands correctly
    refuse.
    """
    identity = get_config().bot
    existing = sandbox.users.get(identity.id)
    if existing is not None:
        return existing
    bot = SandboxUser(
        id=identity.id, first_name=identity.first_name, username=identity.username, is_bot=True
    )
    sandbox.users[bot.id] = bot
    return bot


def _apply_seed(sandbox: SandboxStore, fixture: SeedFixture) -> None:
    """Build a configured world: users, chats, memberships, DMs.

    Ids are minted here rather than declared in the config, for the same
    reason the store mints them anywhere else — a config that hardcoded a chat
    id would collide with the next chat a tester creates by hand, and the
    counters are the only thing guaranteeing that never happens.

    A user listed in `users` but named by no chat's member list is not a
    mistake: an account that exists but has not joined anything is exactly
    what a join-time check (a blocklist, a captcha, a welcome) needs in front
    of it, so that pressing "join" is the whole test rather than something the
    seed already did off-screen.
    """
    if fixture.chats or fixture.dms:
        _ensure_bot_user(sandbox)

    users: dict[str, SandboxUser] = {}
    for spec in fixture.users:
        user = SandboxUser(
            id=sandbox.next_user_id(),
            first_name=spec.first_name,
            username=spec.username,
            last_name=spec.last_name,
            language_code=spec.language_code,
        )
        sandbox.users[user.id] = user
        users[spec.key] = user

    for chat_spec in fixture.chats:
        chat = SandboxChat(
            id=sandbox.next_chat_id(),
            title=chat_spec.title,
            type=cast(ChatType, chat_spec.type),
        )
        sandbox.chats[chat.id] = chat
        if chat_spec.bot_role is not None:
            bot = _ensure_bot_user(sandbox)
            chat.members[bot.id] = Membership(user_id=bot.id, role=cast(Role, chat_spec.bot_role))
        for member in chat_spec.members:
            member_user = users.get(member.user)
            if member_user is None:
                # A member naming a user the fixture never declares is a config
                # typo. Skipping it (loudly) beats 500-ing the seed: the rest
                # of the world is still worth having in front of the tester.
                log.warning(
                    "sandbox.seed.unknown_member",
                    seed=fixture.name,
                    chat=chat_spec.key,
                    user=member.user,
                )
                continue
            chat.members[member_user.id] = Membership(
                user_id=member_user.id, role=cast(Role, member.role), anonymous=member.anonymous
            )

    for key in fixture.dms:
        dm_user = users.get(key)
        if dm_user is None:
            log.warning("sandbox.seed.unknown_dm_user", seed=fixture.name, user=key)
            continue
        _open_private_chat(sandbox, dm_user)


def _open_private_chat(sandbox: SandboxStore, user: SandboxUser) -> SandboxChat:
    """The DM between the bot and `user`, created if it does not exist yet.

    **Its id is the user's own id**, which is not a detail — it is how Telegram
    addresses a private chat, and every handler that DMs someone does it by
    passing a user id straight to `sendMessage` (`bot.send_message(
    ctx.actor.user_id, ...)` in the config menu, for one). A DM allocated an id
    from the sandbox's own chat counter is unreachable by that code no matter
    what the tester clicks, so the whole "the bot answers you privately" half of
    several features could not be exercised at all.

    Creating one stands for the user having pressed Start: Telegram forbids a
    bot from opening a conversation, and `telegram_api._require_chat` reproduces
    that refusal for a user who has no DM here.
    """
    existing = sandbox.chats.get(user.id)
    if existing is not None:
        return existing
    bot = _ensure_bot_user(sandbox)
    chat = SandboxChat(id=user.id, title=user.first_name, type="private")
    sandbox.chats[chat.id] = chat
    chat.members[bot.id] = Membership(user_id=bot.id, role="member")
    chat.members[user.id] = Membership(user_id=user.id, role="member")
    return chat


def _seed_by_name(sandbox: SandboxStore, name: str | None) -> SeedFixture:
    """Resolve and apply a named seed, or the configured default when the
    caller names none."""
    config = get_config()
    resolved = name or config.default_seed
    fixture = config.seed(resolved)
    if fixture is None:
        known = ", ".join(config.seed_names()) or "(none configured)"
        raise HTTPException(
            status_code=400, detail=f"unknown seed {resolved!r}, expected one of {known}"
        )
    _apply_seed(sandbox, fixture)
    return fixture


# --------------------------------------------------------------------- state, reset, seed


@router.get("/state")
async def get_state() -> SandboxSnapshot:
    return _snapshot(store())


@router.post("/reset")
async def reset() -> SandboxSnapshot:
    sandbox = store()
    sandbox.reset()
    _seed_by_name(sandbox, None)
    return _snapshot(sandbox)


@router.post("/seed")
async def seed(request: SeedRequest) -> SandboxSnapshot:
    sandbox = store()
    sandbox.reset()
    _seed_by_name(sandbox, request.scenario)
    return _snapshot(sandbox)


# --------------------------------------------------------------------- files
#
# Real bytes, so image features are validatable at all. See `files.py` for why
# a placeholder blob was not enough.


def _file_out(stored: SandboxFile) -> FileOut:
    return FileOut(
        file_id=stored.file_id,
        file_unique_id=stored.file_unique_id,
        mime_type=stored.mime_type,
        file_name=stored.file_name,
        size=stored.size,
        width=stored.width,
        height=stored.height,
        duration=stored.duration,
    )


@router.post("/files", status_code=201)
async def upload_file(request: UploadFileRequest) -> FileOut:
    """Store a file and hand back the id to attach it with.

    Content-addressed, so uploading the same picture twice returns the same id
    rather than growing the store — which also means a tester can re-attach a
    reference image across a whole session for free.
    """
    try:
        stored = store().store_file(
            _decode_upload(request.data),
            file_name=request.filename,
            declared_mime=request.content_type,
            duration=request.duration,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _file_out(stored)


@router.get("/files/{file_id}")
async def download_file(file_id: str) -> Response:
    """The bytes, with their sniffed content type — what an `<img src>` in the
    web client points at, and what makes a photo in the timeline an actual
    photo instead of a grey rectangle.

    Cached hard: a file id is a content hash, so the bytes behind one can never
    change, and a workbench that re-fetches every image on every poll would
    spend its life redrawing pictures it already has.
    """
    stored = store().files.get(file_id)
    if stored is None:
        raise HTTPException(status_code=404, detail=f"file {file_id} not found")
    return Response(
        content=stored.data,
        media_type=stored.mime_type,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


def _decode_upload(data: str) -> bytes:
    payload = data.split(",", 1)[-1] if data.startswith("data:") else data
    try:
        return base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"not valid base64: {exc}") from exc


# --------------------------------------------------------------------- the kit


@router.get("/kit")
async def get_kit() -> KitOut:
    """Everything the client needs to stop being written for one bot.

    Static for the lifetime of the process — the config is read once — so a
    client fetches this on mount and never again. A run's *counts* are not
    static, which is why `features` is also on `GET /api/state`: this endpoint
    exists to describe the bot, that one to describe the run.
    """
    config = get_config()
    sandbox = store()
    scenarios = sorted(
        (_scenario_out(sandbox, s) for s in sandbox.scenarios.values()),
        key=lambda s: s.started_at,
    )
    return KitOut(
        bot=BotOut(
            id=config.bot.id, username=config.bot.username, first_name=config.bot.first_name
        ),
        config_source=config.source_path or "built-in defaults",
        default_seed=config.default_seed,
        seeds=[
            SeedOut(
                name=fixture.name,
                title=fixture.label(),
                description=fixture.description,
                user_count=len(fixture.users),
                chat_count=len(fixture.chats),
            )
            for fixture in config.seeds
        ],
        presets=[
            PresetOut(
                id=preset.id,
                button=preset.button,
                label=preset.title(),
                seed=preset.seed,
                feature_id=preset.feature_id,
                acting_user=preset.acting_user,
                create_user=preset.create_user,
                chat=preset.chat,
                what_to_do=preset.what_to_do,
                what_to_look_for=preset.what_to_look_for,
            )
            for preset in config.presets
        ],
        commands=[
            CommandOut(
                primary=command.primary,
                canonical=command.key(),
                aliases=list(command.aliases),
                feature_id=command.feature_id,
                title=command.title,
                status=command.status,
                hint=command.hint,
            )
            for command in config.commands
        ],
        features=_features_out(config, scenarios),
    )


@router.get("/features")
async def get_features() -> list[FeatureOut]:
    """Every configured feature with this run's scenarios folded in.

    The endpoint a validation pass reads: one row per feature, each carrying
    which scenarios exercised it and how they ended. Also what a CI job would
    poll to answer "did every feature get checked", which no per-scenario
    result can answer on its own — a feature with zero scenarios is the
    interesting case, and it only exists as a row here.
    """
    sandbox = store()
    scenarios = sorted(
        (_scenario_out(sandbox, s) for s in sandbox.scenarios.values()),
        key=lambda s: s.started_at,
    )
    return _features_out(get_config(), scenarios)


# --------------------------------------------------------------------- scenarios
#
# Not to be confused with a `SeedFixture` (`POST /api/seed`), which names a
# starting *world*. A `SandboxScenario` is orthogonal to that: a named span of
# time layered on top of whatever world is already seeded, so a tester can seed
# once and then open, close, and reopen several of these while poking at the
# same group. A scenario is also what carries a `feature`, which is the axis
# `GET /api/features` groups a whole run along.


@router.post("/scenarios", status_code=201)
async def create_scenario(request: CreateScenarioRequest) -> ScenarioOut:
    sandbox = store()
    scenario_id = request.id or sandbox.next_scenario_id()
    if scenario_id in sandbox.scenarios:
        raise HTTPException(status_code=409, detail=f"scenario {scenario_id} already exists")
    scenario = SandboxScenario(
        id=scenario_id,
        name=request.name,
        description=request.description,
        source=request.source,
        feature=request.feature,
        tags=list(request.tags),
        metadata=dict(request.metadata),
    )
    sandbox.scenarios[scenario.id] = scenario
    if request.activate:
        sandbox.active_scenario_id = scenario.id
    return _scenario_out(sandbox, scenario)


@router.post("/scenarios/{scenario_id}/activate")
async def activate_scenario(scenario_id: str) -> ScenarioOut:
    sandbox = store()
    scenario = _get_scenario(sandbox, scenario_id)
    sandbox.active_scenario_id = scenario.id
    return _scenario_out(sandbox, scenario)


@router.post("/scenarios/deactivate")
async def deactivate_scenario() -> dict[str, None]:
    # No id needed: this clears whatever is active, if anything is. Not a
    # 404-if-nothing-is-active call — "make sure nothing is tagged right now"
    # is a valid thing to ask for even when it's already true.
    store().active_scenario_id = None
    return {"active_scenario_id": None}


@router.post("/scenarios/{scenario_id}/notes")
async def add_scenario_note(scenario_id: str, request: ScenarioNoteRequest) -> ScenarioOut:
    sandbox = store()
    scenario = _get_scenario(sandbox, scenario_id)
    scenario.notes.append({"at": time.time(), "text": request.text, "level": request.level})
    sandbox.publish("scenario", {"id": scenario.id, "action": "note"})
    return _scenario_out(sandbox, scenario)


@router.patch("/scenarios/{scenario_id}")
async def patch_scenario(scenario_id: str, request: ScenarioPatchRequest) -> ScenarioOut:
    sandbox = store()
    scenario = _get_scenario(sandbox, scenario_id)
    if request.status is not None:
        scenario.status = request.status
    if request.description is not None:
        scenario.description = request.description
    if request.feature is not None:
        scenario.feature = request.feature
    if request.tags is not None:
        scenario.tags = list(request.tags)
    if request.metadata is not None:
        # Key-by-key, not a replace: a teardown step recording `outcome` should
        # not erase what setup already put in `metadata` (nodeid, group_id, ...).
        scenario.metadata.update(request.metadata)
    sandbox.publish("scenario", {"id": scenario.id, "action": "update"})
    return _scenario_out(sandbox, scenario)


@router.post("/scenarios/{scenario_id}/end")
async def end_scenario(
    scenario_id: str, request: EndScenarioRequest = _DEFAULT_END_SCENARIO_REQUEST
) -> ScenarioOut:
    sandbox = store()
    scenario = _get_scenario(sandbox, scenario_id)
    scenario.ended_at = time.time()
    if request.status is not None:
        scenario.status = request.status
    elif scenario.status == "running":
        scenario.status = "closed"
    if sandbox.active_scenario_id == scenario.id:
        sandbox.active_scenario_id = None
    sandbox.publish("scenario", {"id": scenario.id, "action": "end"})
    return _scenario_out(sandbox, scenario)


# --------------------------------------------------------------------- events (SSE)


@router.get("/events")
async def stream_events() -> StreamingResponse:
    sandbox = store()
    queue = sandbox.subscribe()

    async def event_stream() -> AsyncIterator[str]:
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except TimeoutError:
                    # Keeps the connection alive through proxies that drop an
                    # idle stream; a leading `:` is an SSE comment, ignored by
                    # every client.
                    yield ": heartbeat\n\n"
                    continue
                data = json.dumps({"kind": event.kind, "payload": event.payload, "at": event.at})
                yield f"data: {data}\n\n"
        finally:
            sandbox.unsubscribe(queue)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# --------------------------------------------------------------------- users, chats


@router.post("/users", status_code=201)
async def create_user(request: CreateUserRequest) -> UserOut:
    sandbox = store()
    user = SandboxUser(
        id=sandbox.next_user_id(),
        first_name=request.first_name,
        username=request.username,
        language_code=request.language_code,
    )
    sandbox.users[user.id] = user
    return _user_out(user)


@router.post("/users/{user_id}/dm", status_code=201)
async def start_bot(user_id: int) -> ChatOut:
    """ "This user presses Start" — opens the DM the bot is then allowed to use.

    Separate from `POST /chats` because a private chat is not a chat a tester
    names and allocates; it is a fact about one user, and its id has to be that
    user's id. Idempotent, so a preset can call it without checking first.
    """
    sandbox = store()
    return _chat_out(_open_private_chat(sandbox, _get_user(sandbox, user_id)))


@router.post("/chats", status_code=201)
async def create_chat(request: CreateChatRequest) -> ChatOut:
    sandbox = store()
    chat = SandboxChat(id=sandbox.next_chat_id(), title=request.title, type=request.type)
    sandbox.chats[chat.id] = chat
    return _chat_out(chat)


# --------------------------------------------------------------------- membership


@router.post("/chats/{chat_id}/join")
async def join_chat(chat_id: int, request: JoinRequest) -> ChatOut:
    sandbox = store()
    chat = _get_chat(sandbox, chat_id)
    user = _get_user(sandbox, request.user_id)
    actor = user if request.by_user_id is None else _get_user(sandbox, request.by_user_id)

    existing = chat.members.get(user.id)
    if existing is not None and existing.role not in ("left", "kicked"):
        raise HTTPException(
            status_code=400, detail=f"user {user.id} is already a member of chat {chat_id}"
        )
    chat.members[user.id] = Membership(user_id=user.id, role="member")

    # The join's own service message has to be *stored*, not merely queued as an
    # update. A handler that answers a join with `message.reply(...)` — the
    # captcha in `core_groupguardian` is the one that matters — sends
    # `reply_to_message_id` pointing at it, and `telegram_api._require_message`
    # looks that id up in the store. Queue-only, and every captcha issuance came
    # back `400 Bad Request: message to reply not found`, which reads as "the
    # feature is broken" when the only broken thing was this line.
    message = sandbox.add_message(
        SandboxMessage(
            message_id=sandbox.next_message_id(),
            chat_id=chat_id,
            from_id=actor.id,
            text=None,
            date=time.time(),
            service={"kind": "join", "user_id": user.id, "by_user_id": request.by_user_id},
        )
    )
    sandbox.queue_update({"message": message.as_telegram(sandbox)})
    sandbox.publish(
        "member",
        {
            "chat_id": chat_id,
            "user_id": user.id,
            "action": "join",
            "by_user_id": request.by_user_id,
        },
    )
    return _chat_out(chat)


@router.post("/chats/{chat_id}/leave")
async def leave_chat(chat_id: int, request: LeaveRequest) -> ChatOut:
    sandbox = store()
    chat = _get_chat(sandbox, chat_id)
    user = _get_user(sandbox, request.user_id)
    membership = chat.members.get(user.id)
    if membership is None or membership.role in ("left", "kicked"):
        raise HTTPException(
            status_code=400, detail=f"user {user.id} is not a member of chat {chat_id}"
        )
    actor = user if request.by_user_id is None else _get_user(sandbox, request.by_user_id)
    membership.role = "left" if request.by_user_id is None else "kicked"

    message = sandbox.add_message(
        SandboxMessage(
            message_id=sandbox.next_message_id(),
            chat_id=chat_id,
            from_id=actor.id,
            text=None,
            date=time.time(),
            service={"kind": "leave", "user_id": user.id, "by_user_id": request.by_user_id},
        )
    )
    sandbox.queue_update({"message": message.as_telegram(sandbox)})
    sandbox.publish(
        "member",
        {
            "chat_id": chat_id,
            "user_id": user.id,
            "action": "leave",
            "by_user_id": request.by_user_id,
        },
    )
    return _chat_out(chat)


@router.post("/chats/{chat_id}/members/{user_id}")
async def patch_member(chat_id: int, user_id: int, request: PatchMemberRequest) -> ChatOut:
    sandbox = store()
    chat = _get_chat(sandbox, chat_id)
    membership = chat.members.get(user_id)
    if membership is None:
        raise HTTPException(
            status_code=404, detail=f"user {user_id} is not a member of chat {chat_id}"
        )
    if request.role is not None:
        membership.role = request.role
    if request.anonymous is not None:
        membership.anonymous = request.anonymous
    sandbox.publish(
        "member",
        {
            "chat_id": chat_id,
            "user_id": user_id,
            "action": "update",
            "role": membership.role,
            "anonymous": membership.anonymous,
        },
    )
    return _chat_out(chat)


# --------------------------------------------------------------------- messages, callbacks


@router.post("/chats/{chat_id}/messages", status_code=201)
async def send_message(chat_id: int, request: SendMessageRequest) -> MessageOut:
    sandbox = store()
    chat = _get_chat(sandbox, chat_id)
    user = _get_user(sandbox, request.user_id)
    membership = chat.members.get(user.id)
    if membership is None:
        raise HTTPException(
            status_code=400, detail=f"user {user.id} is not a member of chat {chat_id}"
        )
    if request.text is None and request.media is None:
        raise HTTPException(status_code=400, detail="message needs text or media")
    if request.anonymous and not membership.anonymous:
        raise HTTPException(
            status_code=400,
            detail=f"user {user.id} has not enabled anonymous mode in chat {chat_id}",
        )
    if (
        request.reply_to_message_id is not None
        and sandbox.message(chat_id, request.reply_to_message_id) is None
    ):
        raise HTTPException(
            status_code=404,
            detail=f"message {request.reply_to_message_id} not found in chat {chat_id}",
        )

    # Telegram's own behaviour for an anonymous admin post: `from` becomes
    # GroupAnonymousBot and `sender_chat` becomes the group. Reproducing it
    # exactly is this sandbox's single most valuable case — an admin check
    # written against `from` rejects the one person most entitled to pass,
    # and no unit test of that handler will ever notice.
    if request.anonymous:
        sender = _ensure_anonymous_bot(sandbox)
        sender_chat_id = chat_id
    else:
        sender = user
        sender_chat_id = None

    message = SandboxMessage(
        message_id=sandbox.next_message_id(),
        chat_id=chat_id,
        from_id=sender.id,
        text=request.text,
        date=time.time(),
        sender_chat_id=sender_chat_id,
        reply_to_message_id=request.reply_to_message_id,
        entities=_command_entities(request.text) if request.text else [],
        media=request.media,
        media_file_id=request.media_file_id,
        media_caption=request.media_caption,
    )
    sandbox.add_message(message)
    payload = message.as_telegram(sandbox)
    sandbox.queue_update({"message": payload})
    sandbox.publish("message", _message_out(message).model_dump())
    return _message_out(message)


@router.post("/chats/{chat_id}/callback")
async def press_callback(chat_id: int, request: CallbackRequest) -> dict[str, Any]:
    sandbox = store()
    _get_chat(sandbox, chat_id)
    user = _get_user(sandbox, request.user_id)
    message = sandbox.message(chat_id, request.message_id)
    if message is None:
        raise HTTPException(
            status_code=404, detail=f"message {request.message_id} not found in chat {chat_id}"
        )
    if message.reply_markup is None:
        raise HTTPException(
            status_code=400, detail=f"message {request.message_id} has no inline keyboard"
        )
    known_data = {
        button.get("callback_data")
        for row in message.reply_markup.get("inline_keyboard", [])
        for button in row
    }
    if request.data not in known_data:
        raise HTTPException(
            status_code=400,
            detail=f"no button with callback_data {request.data!r} on message {request.message_id}",
        )

    callback_query = {
        "id": f"sandbox-cb-{next(_callback_ids)}",
        "from": user.as_telegram(),
        "message": message.as_telegram(sandbox),
        "chat_instance": str(chat_id),
        "data": request.data,
    }
    return sandbox.queue_update({"callback_query": callback_query})
