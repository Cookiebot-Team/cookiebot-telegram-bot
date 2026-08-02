"""fun_ship — `/shippar`, `/ship`, and QA's `/shipp` spelling.

v1: `shipp`, `../COOKIEBOT-Telegram-Group-Bot/Bot/UserRegisters.py:216-250`::

    def shipp(cookiebot, msg, chat_id, language, is_alternate_bot=0):
        react_to_message(msg, '❤️', is_alternate_bot=is_alternate_bot)
        send_chat_action(cookiebot, chat_id, 'typing')
        members = get_members_chat(cookiebot, chat_id)
        if len(msg['text'].split()) >= 3:
            target_a = msg['text'].split()[1]
            target_b = msg['text'].split()[2]
        else:
            random.shuffle(members)
            try:
                target_a = members[0]['user']
                target_b = members[1]['user']
            except IndexError:
                text = i18n.get("no_ship", lang=language)
                send_message(cookiebot, chat_id, text, msg)
                return
            ...
        divorce_prob = str(random.randint(0, 100))
        ship_dynamic = i18n.get_random_line("ship_dynamics.txt", lang=language)
        children_quantity = random.choice(['0', '1', '2', '3'])
        ctx = {...}
        text = i18n.get("ship", lang=language, **ctx)
        send_message(cookiebot, chat_id, text, msg)

Dispatched from `COOKIEBOT.py:214-233`: `/shippar` and `/ship` share the big fun
`elif` arm, so the gate is `funfunctions` and a gated-off group is **answered**
with `notify_fun_off` (`Miscellaneous.py:129-131`), not ignored. Same shape
`fun_random.py` already documents for its own `fun` gate.

Contract: `docs/contracts/fun_ship.md`. QA:
`../Cookiebot-QA/features/fun_ship.feature` -> `qa/features/fun_ship.feature`.

## The one-argument case, where QA and v1 disagree

QA's second scenario is "Create a shipp with one user already tagged —
`/shipp @user1` — the bot should reply with a shipp of user1 and another user in
the group". **v1 does not do that.** Its condition is
`len(msg['text'].split()) >= 3`, so a single argument fails it and both targets
come from the random path; `@user1` is discarded entirely. Two arguments are
used verbatim, none are looked up, and a name that is not a member of the group
ships fine.

AGENTS.md §1 ("v1 code wins for observable behaviour, QA wins for intent") makes
this v1's call: the port ignores a lone argument, `qa/features/fun_ship.feature`
carries the v1-accurate wording with the conflict noted in its header, and
`docs/site/content/docs/feature-map.mdx`'s `fun_ship` row records the divergence.

## `@` is not stripped, and that is v1

The catalog string itself supplies the sigil — `"@%(target_a)s + @%(target_b)s"`
— and v1 substitutes the raw token. `/ship @alice @bob` therefore renders
`@@alice + @@bob` in v1, and does here too. It is a cosmetic quirk of a fun
command, visible to users who have been typing it that way for years, and
"fixing" it would change what every existing group sees. Preserved; the
`no_ship`/random path never has the problem because registered usernames are
stored bare (`cb_core.members.random_usernames`).

## Where the members come from

v1 read its per-chat register through the Java backend
(`get_members_chat`, `UserRegisters.py:14-31`) — a list of username strings
maintained by `check_new_name` on every message. v2's equivalent is
`cb_core.members`, written by `cb_gateway.handlers.members` on the same trigger
(every message) and read here as a single-shard `ORDER BY random() LIMIT 2`.

The observable consequence is the same as v1's: only members who have **spoken
since the bot has been watching** can be shipped. v1's register was populated
exactly the same way and had no bulk import either — `getChatMembers` does not
exist in the Bot API. A fresh group ships nobody until two people talk.
"""

from __future__ import annotations

import contextlib
import random
from typing import Final, cast

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.types import Message, ReactionTypeEmoji

from cb_core import locales, members
from cb_core.textmatch import ParsedCommand
from cb_gateway.context import ChatContext, context_for, t
from cb_gateway.filters import CommandName
from cb_gateway.telemetry import mark_outcome

router = Router(name="ship")

# UserRegisters.py:245 — `random.choice(['0', '1', '2', '3'])`, strings because
# the catalog template interpolates them straight into text.
_CHILDREN_CHOICES: Final[tuple[str, ...]] = ("0", "1", "2", "3")

_MIN_DIVORCE_PROB: Final = 0
_MAX_DIVORCE_PROB: Final = 100  # random.randint(0, 100), inclusive at both ends

_TARGETS: Final = 2


def explicit_targets(parsed: ParsedCommand) -> tuple[str, str] | None:
    """v1's `len(msg['text'].split()) >= 3` branch (`UserRegisters.py:219-221`).

    v1 splits the *whole* message, so the head token counts: three tokens means
    the command plus two arguments. `ParsedCommand.args` has already had the head
    removed, so the equivalent test is two-or-more argument tokens. Extra tokens
    beyond the second are ignored, exactly as v1's positional indexing does.
    """
    tokens = parsed.args.split()
    if len(tokens) < _TARGETS:
        return None
    return tokens[0], tokens[1]


def render(target_a: str, target_b: str, lang: str) -> str:
    """v1's `ctx` dict and `i18n.get("ship", ...)` (`UserRegisters.py:242-250`).

    The three random draws happen here rather than in the caller so the whole
    reply is one testable unit; every one of them matches v1's distribution
    (`random`, not `secrets` — see `fun_dice`'s identical note).
    """
    return locales.get(
        "ship",
        lang,
        target_a=target_a,
        target_b=target_b,
        ship_dynamic=random.choice(locales.lines("ship_dynamics", lang)),
        children_quantity=random.choice(_CHILDREN_CHOICES),
        divorce_prob=str(random.randint(_MIN_DIVORCE_PROB, _MAX_DIVORCE_PROB)),
    )


async def _pick_targets(ctx: ChatContext, parsed: ParsedCommand) -> tuple[str, str] | None:
    explicit = explicit_targets(parsed)
    if explicit is not None:
        return explicit
    picked = await members.random_usernames(ctx.group_id, _TARGETS)
    if len(picked) < _TARGETS:
        # v1's `except IndexError` arm (`UserRegisters.py:227-230`): fewer than
        # two registered members answers with `no_ship` and nothing else.
        return None
    return picked[0], picked[1]


@router.message(F.chat.type != ChatType.PRIVATE, CommandName("ship"))
async def shipp(message: Message, parsed: ParsedCommand | None = None) -> None:
    """`/shippar`, `/ship`, `/shipp` — see the module docstring for the trigger
    and argument semantics, and `docs/contracts/fun_ship.md` for the contract."""
    if parsed is None:  # pragma: no cover - CommandName always injects a match
        return

    ctx = await context_for(cast(Bot, message.bot), message)
    if not ctx.enabled("fun"):
        # v1: notify_fun_off passes `msg` positionally into send_message's
        # `msg_to_reply` (Miscellaneous.py:130) — a reply, not a bare send.
        mark_outcome("refused")
        await message.reply(t(ctx, "fun_off"))
        return

    # v1 reacts before it does anything else, including before it knows whether
    # it can ship at all (`UserRegisters.py:217`), so the heart lands even on the
    # `no_ship` path. Best-effort, like every other reaction in this codebase:
    # v1's own `react_to_message` has no error handling and its caller swallows
    # everything (`COOKIEBOT.py:329`).
    with contextlib.suppress(Exception):
        await message.react(reaction=[ReactionTypeEmoji(emoji="❤️")], is_big=True)

    targets = await _pick_targets(ctx, parsed)
    if targets is None:
        await message.reply(t(ctx, "no_ship"))
        return

    await message.reply(render(targets[0], targets[1], ctx.lang))


__all__ = ["explicit_targets", "render", "router", "shipp"]
