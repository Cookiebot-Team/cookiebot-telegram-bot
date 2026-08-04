"""x_conversational_ai — a group member mentions the bot, or replies to
something it said, and it answers in character.

v1: `Bot/NaturalLanguage.py` (whole file), dispatched from
`../COOKIEBOT-Telegram-Group-Bot/Bot/COOKIEBOT.py:304-308` (text) and
`:155-162` (voice, via `x_speech_to_text`). Read `.specs/features/
x_conversational_ai/spec.md` and `design.md` first — every behavioural
decision here (R1-R9) is settled there, including the four the owner
answered on 2026-08-03. Line references below (`R5.4` etc.) point at
`design.md`.

v1 routed straight to OpenAI with a DAN jailbreak system prompt
(`NaturalLanguage.py:18`) that told the model to ignore its provider's
policies, emit two labelled answers, and — explicitly — invent facts it does
not know rather than admit ignorance. **D-AI-1: none of that is ported.**
`_PERSONA_TEMPLATE` below keeps only the character the jailbreak was
carrying (a furry AI, created by Mekhy, an opinionated friend rather than a
neutral assistant) and inverts the fabrication instruction, since an
instruction to make things up is a defect regardless of what v1 shipped.

Two other v1 defects this file exists to fix:

- **D-AI-5**: v1 injects a replied-to bot message as a **`system`**-role
  entry (`NaturalLanguage.py:24-25`), so any user can plant arbitrary system
  instructions by replying to the bot. `build_messages` puts it in as
  `assistant` instead — no user-controlled content ever gets system
  authority.
- **D-AI-7**: v1 hardcodes `"cookiebot"`/`"@CookieMWbot"`/`"@pawstralbot"`
  as trigger literals (`COOKIEBOT.py:304`, `NaturalLanguage.py:69-70`), so a
  second skin like `bombot` could never be addressed by its own name.
  `MentionsBot` and `strip_trigger_tokens` take the skin's own display name
  and `@username` (`tenancy.registry.by_skin`) instead.

D-AI-2 (the cross-chat few-shot bleed) and D-AI-3 (`.capitalize()` wrecking
proper nouns) are fixed by omission: R6.3 drops the few-shot seeds entirely,
and nothing here ever calls `.capitalize()` on model input or output.
D-AI-6 (the dead `simsimi` NSFW branch) has no v2 equivalent — dropped,
recorded in `docs/contracts/x_conversational_ai.md`, not reproduced here.

R4's per-user streak (v1 parity, `Cooldowns.py:5,24-36`) and R3's per-group
window (v2-additive, D-AI-8) are gates on the *trigger decision* and live in
`ai_reply`, never in `reply_with_ai` — R4.5 requires the voice path
(`x_speech_to_text`, which calls `reply_with_ai` directly) to never spend
the streak, and it never sees these gates at all because they are not part
of the factored-out reply half.
"""

from __future__ import annotations

import re
from typing import cast

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.enums import ChatType
from aiogram.filters import BaseFilter
from aiogram.types import Message
from prometheus_client import Counter

from cb_core import cache, tenancy
from cb_core.llm import router as llm_router
from cb_core.llm.types import LLMBudgetExceededError
from cb_core.llm.types import Message as LlmMessage
from cb_core.logging import get_logger
from cb_core.settings import get_settings
from cb_gateway.context import ChatContext, context_for, t
from cb_gateway.filters import FeatureGate
from cb_gateway.telemetry import mark_outcome

log = get_logger("cb.chat_ai")

router = Router(name="chat_ai")

# R7.1: outcome is one of the six values below, never group_id/user_id —
# AGENTS.md SS7's cardinality rule.
ai_replies_total = Counter(
    "cb_gateway_ai_replies_total",
    "Outcomes of a mention-triggered (or reply-to-bot-triggered) AI reply",
    ["outcome"],
)

# R4.2: per user, not per group — v1's `remaining_responses_ai` is a
# process-global dict keyed only by `msg['from']['id']` (`Cooldowns.py:8`).
_STREAK_KEY_PREFIX = "cb:ai:streak:"
_STREAK_LO = -7
_STREAK_HI = 7
_STREAK_INITIAL = 7
_STREAK_TTL_SECONDS = 86_400

# R3.1: mirrors stickerspam's own key shape (`handlers/stickerspam.py:44-49`),
# per group.
_GROUP_KEY_PREFIX = "cb:ai:"

# R6: the character the DAN jailbreak (`NaturalLanguage.py:18`) was carrying,
# minus the jailbreak scaffolding, the dual-response format and — inverted,
# not preserved — the instruction to fabricate an answer rather than admit
# ignorance. Formatted with the skin's own display name (D-AI-7); no
# few-shot seeds (R6.3, and the D-AI-2 fix that comes from dropping them).
_PERSONA_TEMPLATE = (
    "You are {display_name}, a furry AI created by Mekhy. Talk like a friend "
    "with real opinions -- not a neutral, hedging assistant. Keep it informal "
    "and a little irreverent. Reply in whatever language you were addressed "
    "in. Keep answers short. If you don't know something, say so -- never "
    "invent an answer just to sound sure."
)

# R5.6: v1's own strings, verbatim (`NaturalLanguage.py:26-31`). Any language
# not in this map appends nothing — v1's stray `"\n\n"` for an unknown
# language is not ported.
_BREVITY_LINES = {
    "en": "Try to reduce the answer a lot.",
    "pt": "Tente reduzir bastante a resposta.",
    "es": "Intenta reducir mucho la respuesta.",
}


# ------------------------------------------------------------------ MentionsBot


class MentionsBot:
    """R5.4/D-AI-7: pure predicate over already-resolved values — no
    `Message`, no registry lookup, so it needs neither Telegram nor a
    database. `MentionsBotFilter` below is the impure aiogram wrapper that
    resolves those values and delegates here.
    """

    def __init__(self, *, display_name: str, bot_username: str) -> None:
        self._display_name = display_name.lower()
        self._at_username = f"@{bot_username.lower()}"

    def __call__(self, text: str, *, reply_to_bot_text: str | None) -> bool:
        # v1 evaluates "replied to a bot text" and "mention substring" as
        # independent conditions (`COOKIEBOT.py:304`, an `or`) — preserved:
        # a reply to the bot's own text always matches, regardless of what
        # the reply itself says.
        if reply_to_bot_text is not None:
            return True
        lowered = text.lower()
        return self._display_name in lowered or self._at_username in lowered


def _bot_reply_text(message: Message, bot: Bot) -> str | None:
    """The text of a reply to *this* bot's own message, or `None`.

    Shared by `MentionsBotFilter` (R5.4's "is this a mention") and
    `reply_with_ai` (R5.6's D-AI-5 conversation context) so the identity
    check — the reply's sender is this bot, and it has text — is defined
    exactly once.
    """
    reply = message.reply_to_message
    if reply is None or reply.from_user is None or reply.from_user.id != bot.id:
        return None
    return reply.text


class MentionsBotFilter(BaseFilter):
    """The aiogram filter registered on `ai_reply`: resolves the skin's
    tenant (`tenancy.registry.by_skin`) for its display name, then delegates
    to `MentionsBot`. `bot_username` is the dispatcher-injected kwarg
    (`registry.username(skin)`, `main.py:114`) — never a hardcoded literal,
    which is exactly D-AI-7's fix.
    """

    async def __call__(
        self,
        message: Message,
        bot: Bot | None = None,
        skin: str = tenancy.DEFAULT_TENANT,
        bot_username: str = "",
    ) -> bool:
        tenant = await tenancy.registry.by_skin(skin)
        this_bot = cast(Bot, bot or message.bot)
        mentions = MentionsBot(display_name=tenant.display_name, bot_username=bot_username)
        return mentions(message.text or "", reply_to_bot_text=_bot_reply_text(message, this_bot))


# ------------------------------------------------------------ trigger stripping


def strip_trigger_tokens(text: str, *, display_name: str, bot_username: str) -> str:
    """R5.5/D-AI-3/D-AI-7: remove the same tokens the filter matched on
    (case-insensitively), turn newlines into spaces, `.strip()`.

    Deliberately **no** `.capitalize()` — v1's `.capitalize()`
    (`NaturalLanguage.py:69-70`) lowercases every character after the first,
    wrecking acronyms and proper nouns (D-AI-3).
    """
    result = text
    for token in (display_name, f"@{bot_username}"):
        result = re.sub(re.escape(token), "", result, flags=re.IGNORECASE)
    return result.replace("\n", " ").strip()


def stripped_or_placeholder(text: str, *, display_name: str, bot_username: str) -> str:
    """R5.5/v1 `NaturalLanguage.py:74`: an empty result after stripping
    becomes the literal `"?"`. The caller (`reply_with_ai`) uses this to
    decide whether to skip the model call entirely.
    """
    stripped = strip_trigger_tokens(text, display_name=display_name, bot_username=bot_username)
    return stripped if stripped else "?"


def brevity_line(language: str) -> str:
    """R5.6, verbatim from v1 (`NaturalLanguage.py:26-31`). Any language not
    in `_BREVITY_LINES` — including v1's own `"eng"` key, v2 uses `"en"` —
    appends nothing."""
    return _BREVITY_LINES.get(language, "")


# -------------------------------------------------------------- message assembly


def build_messages(
    *, persona: str, text: str, language: str, reply_to_bot_text: str | None
) -> list[LlmMessage]:
    """R5.6/D-AI-5: the persona is the only `system` message. A replied-to
    bot text goes in as `assistant`, never `system` — v1's
    `NaturalLanguage.py:24-25` is a live prompt-injection hole (any user can
    plant instructions by replying to the bot); this pins the fix.
    """
    messages = [LlmMessage(role="system", content=persona)]
    if reply_to_bot_text is not None:
        messages.append(LlmMessage(role="assistant", content=reply_to_bot_text))
    line = brevity_line(language)
    user_content = f"{text}\n\n{line}" if line else text
    messages.append(LlmMessage(role="user", content=user_content))
    return messages


# ------------------------------------------------------------------------ gates


def _group_key(group_id: int) -> str:
    return f"{_GROUP_KEY_PREFIX}{group_id}"


def _streak_key(user_id: int) -> str:
    return f"{_STREAK_KEY_PREFIX}{user_id}"


async def _bump_group(group_id: int, window_seconds: int) -> int | None:
    """R3.1/R3.3: same fail-open contract as `stickerspam._bump` — `None`
    means "cannot tell", never "assume over the limit"."""
    try:
        return await cache.incr_window(_group_key(group_id), window_seconds)
    except Exception as exc:  # noqa: BLE001 - infra outage must fail open, not raise
        log.warning("chat_ai.group_window_failed", group_id=group_id, error=str(exc))
        return None


async def _spend_streak(user_id: int) -> int | None:
    """R4.3: post-decrement value, exactly v1's order (decrement, then
    test). `cache.bump_clamped` already swallows its own errors and returns
    `None` on any Valkey failure (R4.6)."""
    return await cache.bump_clamped(
        _streak_key(user_id),
        -1,
        lo=_STREAK_LO,
        hi=_STREAK_HI,
        initial=_STREAK_INITIAL,
        ttl_seconds=_STREAK_TTL_SECONDS,
    )


async def _replenish_streak(user_id: int) -> int | None:
    """R4.4: v1's `increase_remaining_responses_ai` (`Cooldowns.py:24-29`),
    called from `replenish` for any group text message that reached this
    router without triggering `ai_reply`."""
    return await cache.bump_clamped(
        _streak_key(user_id),
        1,
        lo=_STREAK_LO,
        hi=_STREAK_HI,
        initial=_STREAK_INITIAL,
        ttl_seconds=_STREAK_TTL_SECONDS,
    )


# --------------------------------------------------------------- reply_with_ai


async def reply_with_ai(
    message: Message, ctx: ChatContext, *, skin: str, bot_username: str, text: str
) -> None:
    """R5.9: the reply-generation half, factored out so `x_speech_to_text`'s
    voice handler can call this directly with the transcript as `text` —
    v1's own structure (`COOKIEBOT.py:161-162` assigns the transcript to
    `msg['text']` and calls the same function it dispatches text triggers
    to).

    Deliberately does **not** touch the per-group window (R3) or the
    per-user streak (R4): those gate the *trigger decision* and are
    `ai_reply`'s job, not the reply's. R4.5 requires the voice path to never
    spend the streak, and factoring the gates out of this function is what
    makes that automatic rather than a flag `x_speech_to_text` has to
    remember to pass.

    Every failure path replies — D-AI-4's fix for v1 catching only three
    OpenAI exception types and leaving every other failure (timeouts
    included) with no reply at all (`NaturalLanguage.py:35-36`).
    """
    bot = cast(Bot, message.bot)
    await bot.send_chat_action(message.chat.id, "typing")  # v1 NaturalLanguage.py:66

    tenant = await tenancy.registry.by_skin(skin)
    stripped = stripped_or_placeholder(
        text, display_name=tenant.display_name, bot_username=bot_username
    )
    if stripped == "?":
        # v1 parity: an empty-after-stripping message answers "?" with no
        # model call at all (`NaturalLanguage.py:74`).
        await message.reply("?")
        ai_replies_total.labels(outcome="empty").inc()
        mark_outcome("answered")
        return

    persona = _PERSONA_TEMPLATE.format(display_name=tenant.display_name)
    messages = build_messages(
        persona=persona,
        text=stripped,
        language=ctx.lang,
        reply_to_bot_text=_bot_reply_text(message, bot),
    )

    try:
        # tenant_id triggers R2's budget check inside the router, ahead of
        # the provider call — this is R5.7's third gate. `system=` is not
        # also passed here: `messages[0]` already carries the persona as the
        # sole system-role entry (build_messages' own contract), and the
        # langchain provider does not fold a `system=` kwarg together with a
        # system-role message the way the hand-rolled anthropic provider
        # does — passing both would double it.
        completion = await llm_router().complete(
            "chat",
            messages,
            group_id=ctx.group_id,
            user_id=message.from_user.id if message.from_user is not None else None,
            tenant_id=tenant.tenant_id,
        )
    except LLMBudgetExceededError:
        # R2.4: over budget is a refusal from a spend query that *succeeded*,
        # not an infra failure — told to the user, not silenced.
        await message.reply(t(ctx, "ai_quota_spent"))
        ai_replies_total.labels(outcome="budget").inc()
        mark_outcome("refused")
        return
    except Exception as exc:  # noqa: BLE001 - D-AI-4: every other failure path still replies
        log.warning("chat_ai.completion_failed", error=str(exc))
        await message.reply(t(ctx, "ai_unavailable"))
        ai_replies_total.labels(outcome="error").inc()
        mark_outcome("refused")
        return

    # R6.4: no [🔓JAILBREAK] split, no regex laundering — the model's text is
    # sent exactly as written.
    await message.reply(completion.text)
    ai_replies_total.labels(outcome="ok").inc()
    mark_outcome("answered")


# --------------------------------------------------------------------- handlers


@router.message(
    F.chat.type != ChatType.PRIVATE,
    F.text,
    FeatureGate("fun"),
    MentionsBotFilter(),
)
async def ai_reply(
    message: Message,
    bot: Bot | None = None,
    skin: str = tenancy.DEFAULT_TENANT,
    bot_username: str = "",
) -> None:
    """R5.3(1): the mention trigger.

    R5.7's gate order, cheapest and most local first: per-group window (R3),
    then the per-user streak (R4); the tenant budget (R2) is checked inside
    `reply_with_ai`'s router call. Registered ahead of `replenish` in this
    same router (R5.2), so a match here consumes the update and `replenish`
    never runs for it — the streak is spent only on the branch that tried to
    trigger the AI, never on the branch that fell through to it, matching
    v1's `if`/`else` (`COOKIEBOT.py:304-313`).

    No `deny_if_disabled` — `FeatureGate("fun")` already declined silently
    when `fun` is off (v1 sends no `fun_off` notice on this path either,
    `COOKIEBOT.py:304`, contrast `:218-219`), and the message still reaches
    `replenish` below since this handler simply never matched.
    """
    ctx = await context_for(cast(Bot, bot or message.bot), message)
    settings = get_settings()

    group_count = await _bump_group(ctx.group_id, settings.ai_chat_window_seconds)
    if group_count is not None and group_count >= settings.ai_chat_group_limit:
        # R3.2: reply once, exactly at the limit; stay silent above it, so a
        # spamming group is not answered with a wall of notices.
        if group_count == settings.ai_chat_group_limit:
            await message.reply(t(ctx, "ai_rate_limited"))
            mark_outcome("refused")
        else:
            mark_outcome("silent")
        ai_replies_total.labels(outcome="rate_limited").inc()
        return

    user_id = message.from_user.id if message.from_user is not None else None
    if user_id is not None:
        streak = await _spend_streak(user_id)
        if streak is not None and streak <= 0:
            # R4.3: the seventh consecutive trigger goes unanswered — v1's
            # own silence (`Cooldowns.py:31-36`, `COOKIEBOT.py:306-307`), not
            # a bug to fix.
            ai_replies_total.labels(outcome="streak_exhausted").inc()
            mark_outcome("silent")
            return

    await reply_with_ai(message, ctx, skin=skin, bot_username=bot_username, text=message.text or "")


@router.message(F.chat.type != ChatType.PRIVATE, F.text)
async def replenish(message: Message) -> None:
    """R5.3(2)/R4.4: every group text message that reaches this router
    without triggering `ai_reply` replenishes the streak — v1's `else`
    (`COOKIEBOT.py:313`). Always yields (`members.py:56-72` is the same
    yield-don't-consume precedent): consuming the update here would silently
    swallow every message downstream (`embedder`, `fun_random`).
    """
    if message.from_user is not None:
        await _replenish_streak(message.from_user.id)
    raise SkipHandler


__all__ = [
    "MentionsBot",
    "MentionsBotFilter",
    "ai_replies_total",
    "ai_reply",
    "brevity_line",
    "build_messages",
    "replenish",
    "reply_with_ai",
    "router",
    "strip_trigger_tokens",
    "stripped_or_placeholder",
]
