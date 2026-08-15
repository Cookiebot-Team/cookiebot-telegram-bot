"""x_group_config_api — the settings menu, as HTTP, for the Mini App.

Everything `/config` can do in a Telegram private chat, an admin can now do
from the Mini App: read the group's settings, change them, set the rules and
the welcome message, and read back who changed what
(`x_audit_log`).

Three rules shape every endpoint here.

**The group is the authorisation boundary.** Nothing is global. Every path
carries `{group_id}`, every query filters on it (AGENTS.md §4), and
`cb_api.security.group_admin_caller` decides — group admins and the tenant's
owners, nobody else. A caller who does not administer the group gets **404**,
the same answer a group that does not exist gets, so a logged-in stranger
cannot walk chat ids.

**Reading and writing are different scopes.** `groups:read` for the GETs,
`groups:write` for the mutations, `audit:read` for the trail. A token from
`/login` carries no scopes and is therefore read-only (`security.LEGACY_SCOPES`)
— the console could not write before this existed and does not start now.

**Every write leaves a row.** The audit entry records the fields that actually
changed, with their old and new values, plus who and from where. A settings
surface with two front ends and no history is how a group ends up unable to
answer "who turned the captcha off".

Every response is a declared model rather than a bare mapping, so
`/openapi.json` describes the shapes as well as the paths: the Mini App is
built against generated clients, and a schema that says `object` would leave
its authors reading this file instead.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from cb_api.security import Caller, current_caller, group_admin_caller
from cb_core import audit, db, group_config, group_texts, locales
from cb_core.logging import get_logger

log = get_logger("cb.api.groups")

router = APIRouter(tags=["groups"])

#: Where a write came from, for the audit row. Everything through this router
#: is one of the two HTTP surfaces; the Telegram side writes `telegram`.
_SURFACE = "miniapp"

_MY_GROUPS = """
SELECT g.group_id, g.title, g.username, g.chat_type, ga.role, ga.anonymous
  FROM group_admins ga
  JOIN groups g ON g.group_id = ga.group_id
 WHERE ga.user_id = $1
   AND g.left_at IS NULL
 ORDER BY g.title NULLS LAST, g.group_id
 LIMIT $2
"""


#: Every spelling the Telegram menu accepts, plus the canonical codes. A code
#: outside this set is a typo, and a typo that resolved to English silently is
#: how a group ends up in the wrong language with nobody having chosen it.
_ACCEPTED_LANGUAGES = frozenset(
    {"en", "eng", "english", "pt", "pt-br", "portuguese", "es", "spanish"}
)


class ErrorBody(BaseModel):
    """FastAPI's error envelope, named so the schema can point at it."""

    detail: str


#: The refusals every group-scoped endpoint here can answer with. Documented on
#: each route rather than assumed: a client that cannot tell 403 from 404 will
#: retry the wrong one, and the difference between them is the whole reason
#: `security.group_admin_caller` orders its checks the way it does.
_GROUP_ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorBody, "description": "no bearer token, or one that did not verify"},
    403: {
        "model": ErrorBody,
        "description": "an admin whose token lacks the scope; ask /oauth2/token for a better one",
    },
    404: {
        "model": ErrorBody,
        "description": "no such group — or the caller does not administer it, which answers alike",
    },
}


class ConfigPatch(BaseModel):
    """The writable half of `group_configs`, every field optional.

    Written out rather than derived from `GroupConfig`'s dataclass fields: this
    is a public contract, and a column added to the table should not silently
    become writable over HTTP. The bounds are the ones the Telegram menu
    enforces by refusing to parse anything else, made explicit.
    """

    model_config = ConfigDict(extra="forbid")

    allow_furbots: bool | None = None
    sticker_spam_limit: int | None = Field(default=None, ge=1, le=100)
    sticker_spam_window_s: int | None = Field(default=None, ge=1, le=3600)
    media_restrict_seconds: int | None = Field(default=None, ge=0, le=604_800)
    captcha_timeout_seconds: int | None = Field(default=None, ge=0, le=86_400)
    functions_fun: bool | None = None
    functions_utility: bool | None = None
    sfw: bool | None = None
    language: str | None = None
    publisher_post: bool | None = None
    publisher_ask: bool | None = None
    publisher_members_only: bool | None = None
    thread_posts: str | None = Field(default=None, max_length=64)
    max_posts: int | None = Field(default=None, ge=0, le=9999)
    doomlist_enabled: bool | None = None

    @field_validator("language")
    @classmethod
    def _language(cls, value: str | None) -> str | None:
        """`pt`, `eng`, `es` and the other spellings the menu takes, stored as
        the canonical code. An unrecognised code is a 422, not a silent fall
        back to English."""
        if value is None:
            return None
        normalised = value.strip().lower()
        if normalised not in _ACCEPTED_LANGUAGES:
            raise ValueError(f"language must be one of: {', '.join(sorted(_ACCEPTED_LANGUAGES))}")
        return locales.resolve_language(normalised)

    def changes(self) -> dict[str, Any]:
        return {k: v for k, v in self.model_dump().items() if v is not None}


class TextBody(BaseModel):
    """The rules, or a welcome message. Length is Telegram's message limit —
    text this endpoint accepts that the bot could never send would be a setting
    that saves and then fails silently in the chat."""

    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1, max_length=4096)


class GroupConfigValues(BaseModel):
    """The group's effective settings — the same fifteen columns `ConfigPatch`
    writes, none of them optional here because a read always resolves to a
    value (stored, else the tenant's default, else v1's)."""

    allow_furbots: bool
    sticker_spam_limit: int
    sticker_spam_window_s: int
    media_restrict_seconds: int
    captcha_timeout_seconds: int
    functions_fun: bool
    functions_utility: bool
    sfw: bool
    language: str
    publisher_post: bool
    publisher_ask: bool
    publisher_members_only: bool
    thread_posts: str | None = Field(
        default=None, description="the pinned topic id, or null for none (v1 wrote '9999')"
    )
    max_posts: int
    doomlist_enabled: bool


class ConfigResponse(BaseModel):
    group_id: int
    config: GroupConfigValues


class ConfigUpdateResponse(ConfigResponse):
    changed: list[str] = Field(
        description="the fields whose value actually moved — a patch that asks for "
        "the value a setting already has changes nothing and audits nothing"
    )


class GroupTextResponse(BaseModel):
    """The rules or the welcome message, with its provenance. `body` is null
    when the group never set one; that is the normal state, not a 404."""

    group_id: int
    body: str | None
    updated_by: int | None
    updated_at: datetime | None


class AdministeredGroup(BaseModel):
    group_id: int
    title: str | None
    username: str | None
    chat_type: str | None
    role: str = Field(description="what `group_admins` says: `creator` or `administrator`")
    anonymous: bool | None


class MeResponse(BaseModel):
    user_id: int
    scopes: list[str]
    audience: str | None = Field(default=None, description="the token's `aud`, if it carries one")
    groups: list[AdministeredGroup]


class AuditEvent(BaseModel):
    id: str = Field(description="UUIDv7 — ordering by it is ordering by time")
    ts: datetime
    action: str = Field(description="`config.updated`, `rules.updated`, `welcome.updated`, …")
    surface: str = Field(description="`telegram`, `miniapp`, `api` or `system`")
    actor_user_id: int | None = Field(
        default=None, description="null when an anonymous admin made the change"
    )
    actor_kind: str
    summary: str | None
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    trace_id: str | None


class AuditPage(BaseModel):
    group_id: int
    events: list[AuditEvent]
    next_before: str | None = Field(
        default=None,
        description="pass as `before` for the following page; null on the last one",
    )


def _config_dict(config: group_config.GroupConfig) -> dict[str, Any]:
    return {
        field: getattr(config, field)
        for field in (
            "allow_furbots",
            "sticker_spam_limit",
            "sticker_spam_window_s",
            "media_restrict_seconds",
            "captcha_timeout_seconds",
            "functions_fun",
            "functions_utility",
            "sfw",
            "language",
            "publisher_post",
            "publisher_ask",
            "publisher_members_only",
            "thread_posts",
            "max_posts",
            "doomlist_enabled",
        )
    }


# ------------------------------------------------------------------------ me


@router.get(
    "/me",
    summary="The caller, their scopes, and the groups they administer",
    response_model=MeResponse,
    responses={401: _GROUP_ERRORS[401]},
)
async def me(caller: Annotated[Caller, Depends(current_caller)]) -> dict[str, Any]:
    """Who the token says you are, what it lets you do, and which groups you
    administer.

    The Mini App's first call: it has `initData` telling it who the user is,
    but not which of that user's groups this deployment knows about. The list
    comes from `group_admins`, which the gateway maintains — so a promotion
    made a minute ago appears once the bot has seen an admin-gated command in
    that group, and not before (`cb_api.security`'s note on why this service
    never calls Telegram to refresh it).
    """
    rows = await db.fetch(_MY_GROUPS, caller.user_id, 200, name="api_my_groups")
    return {
        "user_id": caller.user_id,
        "scopes": sorted(caller.scopes),
        "audience": caller.audience,
        "groups": [
            {
                "group_id": row["group_id"],
                "title": row["title"],
                "username": row["username"],
                "chat_type": row["chat_type"],
                "role": row["role"],
                "anonymous": row["anonymous"],
            }
            for row in rows
        ],
    }


# -------------------------------------------------------------------- config


@router.get(
    "/groups/{group_id}/config",
    summary="Read a group's settings",
    response_model=ConfigResponse,
    responses=_GROUP_ERRORS,
)
async def read_config(
    group_id: Annotated[int, Path()],
    _caller: Annotated[Caller, Depends(group_admin_caller("groups:read"))],
) -> dict[str, Any]:
    """The group's effective settings — stored values over tenant defaults over
    v1's defaults, which is the same resolution the bot itself reads."""
    config = await group_config.get_config(group_id)
    return {"group_id": group_id, "config": _config_dict(config)}


@router.patch(
    "/groups/{group_id}/config",
    summary="Change some of a group's settings",
    response_model=ConfigUpdateResponse,
    responses={
        400: {"model": ErrorBody, "description": "the patch carried no settings"},
        **_GROUP_ERRORS,
    },
)
async def update_config(
    group_id: Annotated[int, Path()],
    patch: ConfigPatch,
    caller: Annotated[Caller, Depends(group_admin_caller("groups:write"))],
) -> dict[str, Any]:
    """Change some settings. Absent fields are left alone — this is a PATCH,
    not a PUT, because a Mini App form that round-trips every column would
    overwrite a change another admin made while it was open.

    An empty patch is a 400 rather than a no-op success: a client that sent
    nothing meant to send something.
    """
    changes = patch.changes()
    if not changes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="no settings in the request"
        )

    before = _config_dict(await group_config.get_config(group_id))
    updated = await group_config.set_config(group_id, **changes)
    after = _config_dict(updated)

    changed_before, changed_after = audit.diff(before, after)
    if changed_after:
        await audit.record(
            group_id,
            audit.CONFIG_UPDATED,
            actor_user_id=caller.user_id,
            surface=_SURFACE,
            summary=f"changed {', '.join(sorted(changed_after))}",
            before=changed_before,
            after=changed_after,
        )
    log.info("api.config.updated", group_id=group_id, fields=",".join(sorted(changes)))
    return {"group_id": group_id, "config": after, "changed": sorted(changed_after)}


# ------------------------------------------------------------- rules, welcome


@router.get(
    "/groups/{group_id}/rules",
    summary="Read the group's rules",
    response_model=GroupTextResponse,
    responses=_GROUP_ERRORS,
)
async def read_rules(
    group_id: Annotated[int, Path()],
    _caller: Annotated[Caller, Depends(group_admin_caller("groups:read"))],
) -> dict[str, Any]:
    return _text_response(group_id, await group_texts.get_rules(group_id))


@router.put(
    "/groups/{group_id}/rules",
    summary="Set the group's rules — what /newrules does, over HTTP",
    response_model=GroupTextResponse,
    responses=_GROUP_ERRORS,
)
async def write_rules(
    group_id: Annotated[int, Path()],
    payload: TextBody,
    caller: Annotated[Caller, Depends(group_admin_caller("groups:write"))],
) -> dict[str, Any]:
    """What `/newrules` sets, set from the Mini App instead. Same table, same
    upsert (`cb_core.group_texts`), so `/rules` in the chat shows this text
    immediately."""
    previous = await group_texts.get_rules(group_id)
    await group_texts.set_rules(group_id, payload.body, updated_by=caller.user_id)
    await audit.record(
        group_id,
        audit.RULES_UPDATED,
        actor_user_id=caller.user_id,
        surface=_SURFACE,
        summary="rules updated",
        before={"body": previous.body} if previous else {"body": None},
        after={"body": payload.body},
    )
    return _text_response(group_id, await group_texts.get_rules(group_id))


@router.get(
    "/groups/{group_id}/welcome",
    summary="Read the group's welcome message",
    response_model=GroupTextResponse,
    responses=_GROUP_ERRORS,
)
async def read_welcome(
    group_id: Annotated[int, Path()],
    _caller: Annotated[Caller, Depends(group_admin_caller("groups:read"))],
) -> dict[str, Any]:
    return _text_response(group_id, await group_texts.get_welcome(group_id))


@router.put(
    "/groups/{group_id}/welcome",
    summary="Set the group's welcome message — what /newwelcome does, over HTTP",
    response_model=GroupTextResponse,
    responses=_GROUP_ERRORS,
)
async def write_welcome(
    group_id: Annotated[int, Path()],
    payload: TextBody,
    caller: Annotated[Caller, Depends(group_admin_caller("groups:write"))],
) -> dict[str, Any]:
    """`<user>` and its eight sibling spellings are substituted when the message
    is sent, not here — the body is stored exactly as given, which is what
    `/newwelcome` does."""
    previous = await group_texts.get_welcome(group_id)
    await group_texts.set_welcome(group_id, payload.body, updated_by=caller.user_id)
    await audit.record(
        group_id,
        audit.WELCOME_UPDATED,
        actor_user_id=caller.user_id,
        surface=_SURFACE,
        summary="welcome message updated",
        before={"body": previous.body} if previous else {"body": None},
        after={"body": payload.body},
    )
    return _text_response(group_id, await group_texts.get_welcome(group_id))


def _text_response(group_id: int, record: group_texts.GroupText | None) -> dict[str, Any]:
    """A group that never set one answers with `body: null` rather than 404:
    "not set" is the normal state of a welcome message, not a missing resource.
    """
    return {
        "group_id": group_id,
        "body": record.body if record else None,
        "updated_by": record.updated_by if record else None,
        "updated_at": record.updated_at if record else None,
    }


# --------------------------------------------------------------------- audit


@router.get(
    "/groups/{group_id}/audit",
    summary="Read the group's audit trail, newest first",
    response_model=AuditPage,
    responses=_GROUP_ERRORS,
)
async def read_audit(
    group_id: Annotated[int, Path()],
    _caller: Annotated[Caller, Depends(group_admin_caller("audit:read"))],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    before: Annotated[UUID | None, Query(description="last id of the previous page")] = None,
    action: Annotated[str | None, Query(max_length=64)] = None,
    actor_user_id: Annotated[int | None, Query()] = None,
) -> dict[str, Any]:
    """The group's trail, newest first, keyset-paginated (D11 — no OFFSET, no
    unbounded list).

    `next_before` is the cursor for the following page and is `null` on the last
    one. Filtering by `action` or `actor_user_id` narrows without changing the
    cursor's meaning.
    """
    events = await audit.page(
        group_id,
        limit=limit,
        before_id=before,
        action=action,
        actor_user_id=actor_user_id,
    )
    return {
        "group_id": group_id,
        "events": [
            {
                "id": str(event.id),
                "ts": event.ts,
                "action": event.action,
                "surface": event.surface,
                "actor_user_id": event.actor_user_id,
                "actor_kind": event.actor_kind,
                "summary": event.summary,
                "before": event.before,
                "after": event.after,
                "trace_id": event.trace_id,
            }
            for event in events
        ],
        "next_before": str(events[-1].id) if len(events) == limit else None,
    }


__all__ = [
    "AuditPage",
    "ConfigPatch",
    "ConfigResponse",
    "ConfigUpdateResponse",
    "GroupConfigValues",
    "GroupTextResponse",
    "MeResponse",
    "TextBody",
    "router",
]
