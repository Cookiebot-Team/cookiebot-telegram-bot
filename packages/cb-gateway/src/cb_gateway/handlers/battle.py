"""fun_battle — `/battle` (aliased `/batalha`, `/batalla`).

v1: `battle`, `../COOKIEBOT-Telegram-Group-Bot/Bot/SocialContent.py:294-379`,
dispatched `COOKIEBOT.py:216,218-219,224-225` under the shared `functionsFun`
gate. Full behaviour contract: `docs/contracts/fun_battle.md`. Design:
`.specs/features/fun_battle/{spec,design}.md`.

v1 hides three shapes behind one trigger, picked by how many `@tags`
`get_members_tagged` finds and whether the literal word `"random"` appears
anywhere in the message:

* **two people** — two or more `@tags` (only the first two are ever used),
  or `"random"` (which, when both are present, wins over the explicit tags
  — v1 checks `'random' in text` a second time once it has already decided
  this is the two-people shape, `SocialContent.py:298-299`). v1 fetched each
  side's photo by scraping `https://telegram.me/{username}`'s HTML with
  BeautifulSoup, then round-tripped it through OpenCV to a **hardcoded,
  non-namespaced local file** (`user1.jpg`/`user2.jpg`) — a genuine
  cross-request race (D-BT-1) on top of a fragile, undocumented dependency
  (D-BT-2). Neither is ported: this port resolves a tag against
  `cb_core.members.roster` (the same registry `fun_ship`/`util_everyone`
  already read) to a real `user_id`, then calls the Bot API's
  `get_user_profile_photos` — the exact call v1's own no-tag path already
  used — and hands the returned `file_id` straight to a new media group. No
  download, no re-encode, no temp file, so D-BT-1 has nothing left to race
  over. **Accepted behavioural drift**: a tagged user who has never spoken
  in this group cannot be resolved this way, where v1's scrape sometimes
  could reach such a user's public web-preview page. This is not a new
  failure — an unresolvable tag falls into v1's own `battle_extract`
  message, the same string a failed scrape already produced, naming the
  same tagged text.
* **one tag** and **no tag** — v1 falls through to pit that one person (or
  the caller) against a randomly-picked "fighter" character image from
  `bloblist_fighters_eng`/`bloblist_fighters_pt`
  (`SocialContent.py:24-25`) — the exact same private GCS bucket
  `fun_death`'s `bloblist_death` reads from, a different prefix
  (`Fight/English`/`Fight/Portuguese` instead of `Death`). Never checked
  into the v1 repo, no credential anywhere in this environment — see
  `.specs/features/fun_death/spec.md`'s "The blocker" for the evidence, which
  applies unchanged here. **Until that bucket is exported**, both shapes
  reply `battle_no_picture` — v1's own, already-ported "you need a profile
  picture (or it's private)" string, reused verbatim rather than inventing
  a new one, because it is a literally true description of "there is no
  fighter image to use." This is a deliberate, temporary route into an
  existing v1 failure branch, not the final behaviour for these two shapes —
  replaced once `cb_core/asset_data/fight/` exists, the same way
  `fun_death`'s design already plans for its own pool.

D-BT-3 (a v1 crash: `battle_extract`'s `%(user)s` substitution read
`members_tagged[0]`, which is out of range whenever the `"random"` branch
was reached with zero explicit `@` tags in the message, e.g. bare
`/battle random`) has no equivalent here — this port always names whichever
side actually failed, never an index into a list that may not apply.
"""

from __future__ import annotations

import contextlib
import random
from dataclasses import dataclass
from enum import Enum, auto
from typing import cast

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.types import InputMediaPhoto, InputPollOption, MediaUnion, Message, ReactionTypeEmoji

from cb_core import locales, members
from cb_core.members import MemberRef
from cb_gateway.context import context_for, t
from cb_gateway.filters import CommandName
from cb_gateway.telemetry import mark_outcome

router = Router(name="battle")


class BattleShape(Enum):
    """Which of v1's three code paths a message selects (design R1.3)."""

    TWO_PEOPLE = auto()
    ONE_TAG = auto()
    SELF = auto()


# --------------------------------------------------------------- target parsing


def parse_tagged_targets(text: str) -> list[str]:
    """v1's `get_members_tagged` (`SocialContent.py:104-111`), byte-for-byte.

    Splits on every `"@"` and drops the head, so a target can carry
    trailing text up to the next `"@"` or the end of the string — v1's own
    quirk, not trimmed here either, since it is what a caption/poll choice
    actually displays (design R1.1, R4.2). Anything ending in the literal
    lowercase substring `"bot"` is dropped — case-sensitive, matching v1's
    `.endswith('bot')` exactly. Because of the trailing-text quirk above,
    this filter effectively only ever catches the **last** tag in a
    message: any earlier one carries trailing text up to the next `"@"`
    and essentially never ends in exactly `"bot"`. Preserved warts and all
    — it is what v1 actually does, not what the filter was presumably meant
    to do.
    """
    if "@" not in text:
        return []
    return [target for target in text.split("@")[1:] if not target.endswith("bot")]


def _leading_token(raw: str) -> str:
    """A raw capture from `parse_tagged_targets`, reduced to something that
    could plausibly be a username — used only for the roster lookup (design
    R1.2). Never shown to a user; the raw string is what appears in captions.
    """
    words = raw.strip().split()
    return words[0] if words else ""


def uses_random(text: str) -> bool:
    """v1: `'random' in msg['text'].lower()` (`SocialContent.py:298-299`) —
    checked against the *whole* message text, not just the arguments, and
    checked twice in v1 (design R2.0): once to decide `TWO_PEOPLE` at all,
    once more to let it win over two explicit tags when both are present.
    """
    return "random" in text.lower()


def battle_shape(text: str, tags: list[str]) -> BattleShape:
    """v1: `if len(members_tagged) > 1 or 'random' in text: ... elif
    len(members_tagged): ... else: ...` (`SocialContent.py:298,345,358`)."""
    if len(tags) > 1 or uses_random(text):
        return BattleShape.TWO_PEOPLE
    if len(tags) == 1:
        return BattleShape.ONE_TAG
    return BattleShape.SELF


def _find_in_roster(roster: tuple[MemberRef, ...], raw: str) -> int | None:
    """The accepted redesign's resolution step (design R2.1): a
    case-insensitive username match against the group's own roster, since
    Telegram usernames are canonically case-insensitive and v1's scrape got
    that for free from `telegram.me`'s own routing.
    """
    token = _leading_token(raw).lower()
    if not token:
        return None
    for member in roster:
        if member.username and member.username.lower() == token:
            return member.user_id
    return None


def pick_two_random(
    candidates: list[MemberRef], rng: random.Random | None = None
) -> list[MemberRef] | None:
    """v1: shuffle-and-retry over the whole roster for two rows with a
    `'user'` key (`SocialContent.py:305-309`); `rng.sample` over a
    pre-filtered candidate list reaches the same result in one call instead
    of an up-to-100-iteration loop (design R2.2) — `None` when fewer than
    two eligible members exist, v1's `battle_no` trigger.

    `rng` follows `firecracker.py`'s convention: `None` in production (the
    shared `random` module state), a seeded `random.Random` in tests.
    """
    if len(candidates) < 2:
        return None
    sampler = rng.sample if rng is not None else random.sample
    return sampler(candidates, 2)


# --------------------------------------------------------------- catalog reads


def _catalog_choice(lang: str, key: str, rng: random.Random | None = None) -> str:
    """A random pick from a top-level catalog **list** value
    (`battle_type`/`battle_rule`/`battle_equip`).

    `cb_core.locales.get` is typed `-> str` and returns a catalog value
    unexamined when no `%` substitutions are requested — correct for every
    flat string key, but these three are JSON arrays, not strings.
    `groupguardian.py:108-125`'s `_captcha_strings` already established the
    fix for this shape (there, a nested object rather than a list): cast
    `locales.catalog(lang)`, reach in by hand, fall back to `en` the same
    way `locales.get` would. Out of this port's file ownership to fix in
    `cb_core/locales.py` itself, same reasoning that docstring gives.
    """
    raw = cast(dict[str, object], locales.catalog(lang))
    value = raw.get(key)
    if not isinstance(value, list):
        raw_en = cast(dict[str, object], locales.catalog("en"))
        value = raw_en.get(key, [])
    picker = rng.choice if rng is not None else random.choice
    return picker(cast(list[str], value))


def flavour_suffix(lang: str, rng: random.Random | None = None) -> str:
    """v1: `battle_full` with three random picks (`SocialContent.py:335-340`)."""
    return locales.get(
        "battle_full",
        lang,
        type=_catalog_choice(lang, "battle_type", rng),
        rule=_catalog_choice(lang, "battle_rule", rng),
        equip=_catalog_choice(lang, "battle_equip", rng),
    )


# --------------------------------------------------------------- caption assembly


@dataclass(frozen=True, slots=True)
class BattleCaption:
    caption: str
    choices: tuple[str, str]


def build_caption(a: str, b: str, *, at_prefix: bool, suffix: str) -> BattleCaption:
    """v1's two shapes (`SocialContent.py:328-333`) — the `@`-prefix
    inconsistency between them is preserved, not normalised (design R4.2):
    explicit tags never get one back (they never carried one — `@` was the
    split delimiter), a `"random"` pick always does. Poll choices never
    carry `@` either way.
    """
    caption = f"@{a} VS @{b}" if at_prefix else f"{a} VS {b}"
    return BattleCaption(caption=caption + suffix, choices=(a, b))


# --------------------------------------------------------------------- handler


@router.message(F.chat.type != ChatType.PRIVATE, CommandName("battle"))
async def battle(message: Message, bot: Bot | None = None) -> None:
    """`/battle`, `/batalha`, `/batalla` (aliased). See the module docstring
    for the three shapes and what changed against v1."""
    active_bot = cast(Bot, bot or message.bot)
    ctx = await context_for(active_bot, message)

    # v1 reacts before anything else, including before it knows whether it
    # can battle at all (`:295`) — best-effort like every other reaction in
    # this codebase.
    with contextlib.suppress(Exception):
        await message.react(reaction=[ReactionTypeEmoji(emoji="🔥")])

    if not ctx.enabled("fun"):
        mark_outcome("refused")
        await message.reply(t(ctx, "fun_off"))
        return

    await active_bot.send_chat_action(ctx.group_id, "upload_photo")

    text = message.text or ""
    tags = parse_tagged_targets(text)
    shape = battle_shape(text, tags)

    if shape is not BattleShape.TWO_PEOPLE:
        # ONE_TAG / SELF both need a "fighter" opponent image this port does
        # not have yet (fun_death's identical Fight/-prefix GCS blocker,
        # .specs/features/fun_death/spec.md). Reusing v1's own
        # battle_no_picture — "you need a profile picture ... or it's
        # private" — is a temporary route into an existing failure branch,
        # not the final behaviour for these two shapes (module docstring).
        await message.reply(t(ctx, "battle_no_picture"))
        return

    roster = await members.roster(ctx.group_id)

    at_prefix: bool
    pairs: list[tuple[str, int]]
    if uses_random(text):
        candidates = [member for member in roster if member.username]
        picked = pick_two_random(candidates)
        if picked is None:
            await message.reply(t(ctx, "battle_no"))
            return
        pairs = [(cast(str, member.username), member.user_id) for member in picked]
        at_prefix = True
    else:
        pairs = []
        for raw in (tags[0], tags[1]):
            resolved = _find_in_roster(roster, raw)
            if resolved is None:
                await message.reply(t(ctx, "battle_extract", user=raw))
                return
            pairs.append((raw, resolved))
        at_prefix = False

    file_ids: list[str] = []
    for display, user_id in pairs:
        photos = await active_bot.get_user_profile_photos(user_id, limit=1)
        if not photos.photos:
            await message.reply(t(ctx, "battle_extract", user=display))
            return
        file_ids.append(photos.photos[0][-1].file_id)

    built = build_caption(
        pairs[0][0], pairs[1][0], at_prefix=at_prefix, suffix=flavour_suffix(ctx.lang)
    )
    media: list[MediaUnion] = [
        InputMediaPhoto(media=file_ids[0], caption=built.caption),
        InputMediaPhoto(media=file_ids[1]),
    ]
    await message.reply_media_group(media)
    await message.reply_poll(
        t(ctx, "battle_title"),
        [InputPollOption(text=built.choices[0]), InputPollOption(text=built.choices[1])],
        is_anonymous=False,
        allows_multiple_answers=False,
    )


__all__ = [
    "BattleCaption",
    "BattleShape",
    "battle",
    "battle_shape",
    "build_caption",
    "flavour_suffix",
    "parse_tagged_targets",
    "pick_two_random",
    "router",
    "uses_random",
]
