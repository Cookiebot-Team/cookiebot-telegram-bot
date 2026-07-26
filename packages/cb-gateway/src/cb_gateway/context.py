"""One call that gives a handler everything v1 recomputed per message.

v1's dispatcher rebuilt this state inline for every update: `get_config` (a
process-local dict, five processes, no TTL), `get_admins` (same), and a language
string threaded through every function signature by hand — thirteen positional
values unpacked at `COOKIEBOT.py:113` and passed down. Here it is one awaited
call, and the caching lives in the layers that own it.

Handlers should reach for `context_for()` and `t()` and nothing else: the point
is that no handler knows whether the config came from L1, valkey, Postgres or the
defaults, or how an anonymous admin was resolved.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from aiogram import Bot
from aiogram.types import CallbackQuery, InaccessibleMessage, Message

from cb_core import admins, group_config, locales
from cb_core.admins import ActorCheck
from cb_core.group_config import GroupConfig
from cb_core.telemetry import span


@dataclass(frozen=True, slots=True)
class ChatContext:
    group_id: int
    config: GroupConfig
    lang: str
    actor: ActorCheck

    @property
    def is_admin(self) -> bool:
        return self.actor.is_admin

    def enabled(self, area: str) -> bool:
        """`'fun'` / `'utility'` — v1's functionsFun / functionsUtility gates."""
        return self.config.feature_enabled(area)


async def context_for(bot: Bot, event: Message | CallbackQuery) -> ChatContext:
    """Config, language and actor for the chat this event belongs to.

    Never raises for infrastructure reasons: `group_config` falls back to the v1
    defaults and `admins` falls back to the persisted set, because a database or
    Telegram hiccup must not turn into silence in the group (AGENTS.md §2.6).
    """
    # Nearly every handler's first line — one child span for "where did the
    # config/tenant read and the admin resolution go" beats every handler
    # wrapping this call itself, and matches this module's own pitch: a handler
    # calls `context_for()` and nothing else.
    with span("gateway.context_for"):
        # A callback's `.message` is `None` only for an inline-mode press (no chat
        # to resolve a config/actor for); every call site here feeds a real
        # in-chat button press, so this cast documents an existing assumption
        # rather than adding a new one — an inline-mode callback would still fail
        # the same way (an AttributeError from `.chat`) it did before this
        # annotation.
        message = cast(
            Message | InaccessibleMessage,
            event.message if isinstance(event, CallbackQuery) else event,
        )
        group_id = message.chat.id
        config = await group_config.get_config(group_id)
        if isinstance(event, CallbackQuery):
            # A callback carries the presser in `from_user` and the chat only on
            # the message it belongs to, so `resolve_actor` (which reads
            # `message.chat`) cannot be handed the query directly. An anonymous
            # admin's press arrives as the GroupAnonymousBot, same id as an
            # anonymous post.
            presser = event.from_user
            anonymous = presser is not None and presser.id == admins.ANONYMOUS_BOT_ID
            actor = ActorCheck(
                user_id=None if anonymous or presser is None else presser.id,
                is_admin=anonymous
                or (presser is not None and await admins.is_admin(bot, group_id, presser.id)),
                anonymous=anonymous,
            )
        else:
            # cb_core stays framework-agnostic and reads `event` through a
            # structural `_MessageLike` protocol; mypy checks a Protocol's plain
            # (non-method) attributes invariantly, so aiogram's concrete `Message`
            # never satisfies it even though every field it reads is present. A
            # real aiogram payload at a real cb_core seam — the documented `Any`
            # carve-out (pyproject.toml's `ANN401` note), not a genuine type gap.
            actor = await admins.resolve_actor(bot, cast(Any, event))
        return ChatContext(
            group_id=group_id,
            config=config,
            lang=locales.resolve_language(config.language),
            actor=actor,
        )


async def deny_if_disabled(message: Message, ctx: ChatContext, area: str) -> bool:
    """Reply with v1's "feature is off" notice and return True when `area` is off.

    v1's dispatcher answers a gated-off *command* rather than ignoring it
    (`notify_fun_off` / `notify_utility_off`, `COOKIEBOT.py:218,252`), and the
    strings are already in the ported catalog. Command handlers call this first;
    passive features use the `FeatureGate` filter, which is silent by design.

        if await deny_if_disabled(message, ctx, "fun"):
            return
    """
    if ctx.enabled(area):
        return False
    await message.reply(t(ctx, "fun_off" if area == "fun" else "utility_off"))
    return True


def t(ctx: ChatContext, key: str, **fmt: object) -> str:
    """The group's own language, falling back to en — v1's `lib.json` lookup."""
    return locales.get(key, ctx.lang, **fmt)
