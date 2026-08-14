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
  (`Fight/English`/`Fight/Portuguese` instead of `Death`). Both shapes
  shipped a temporary `battle_no_picture` reply while that bucket was
  unreachable; it has since been exported and catalogued
  (`cb_worker.bucket_export`, `cb.py legacy-catalog`), so they now do what
  v1 does, reading their 711 English and 114 Portuguese fighters through
  `legacy_assets.choose` exactly as `death.py` reads its own pool. The one
  substitution is how the *human* side's photo is found: the roster plus
  `get_user_profile_photos`, not a `telegram.me` scrape — the same accepted
  drift the two-people shape already documents, reaching v1's own
  `battle_private` string when a tag cannot be resolved.

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
from posixpath import splitext
from typing import cast

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.types import (
    BufferedInputFile,
    InputMediaPhoto,
    InputPollOption,
    MediaUnion,
    Message,
    ReactionTypeEmoji,
)

from cb_core import legacy_assets, locales, members, storage
from cb_core.logging import get_logger
from cb_core.members import MemberRef
from cb_gateway.context import ChatContext, context_for, t
from cb_gateway.filters import CommandName
from cb_gateway.telemetry import mark_outcome

router = Router(name="battle")

log = get_logger("cb.gateway.battle")

#: v1's two fighter pools (`SocialContent.py:24-25`) — the same literal
#: prefixes `bucket_export.PREFIXES` exported and `legacy_assets` keys its
#: catalogs on.
_FIGHT_EN = "Fight/English"
_FIGHT_PT = "Fight/Portuguese"


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


def fighter_pool_prefix(lang: str, rng: random.Random | None = None) -> str:
    """Which `Fight/` catalog this language draws from — v1's own asymmetry
    (`SocialContent.py:366-370`): a `pt` group draws from *either* pool
    (`random.choice(random.choice([eng, pt]))`, so the pool is picked first
    and each pool is equally likely regardless of how many fighters it
    holds — 711 English against 114 Portuguese, and a `pt` group still sees
    a Portuguese fighter half the time), every other language draws from
    English only. Preserved: it is what the feature actually does, and the
    weighting is the point of the branch rather than an accident of it.
    """
    if lang != "pt":
        return _FIGHT_EN
    picker = rng.choice if rng is not None else random.choice
    return picker([_FIGHT_EN, _FIGHT_PT])


def fighter_display_name(source_path: str) -> str:
    """v1: `fighter.name.split('/')[-1].replace(".png", "").replace(".jpg",
    "").replace(".jpeg", "").replace("_", " ").capitalize()`
    (`SocialContent.py:373`) — there is no locale string for fighter names,
    they are however the file happened to be named in the bucket.

    Ported verbatim, including both of its warts: only those three
    extensions are stripped (a `.gif` fighter keeps its extension in the
    poll option), and `.capitalize()` **lowercases everything after the
    first character**, so `Darth_Vader.png` becomes `Darth vader`. Neither
    is a defect worth fixing — the name is user-visible text in a fun
    command, and changing it would change what a poll option reads.
    """
    name = source_path.split("/")[-1]
    for extension in (".png", ".jpg", ".jpeg"):
        name = name.replace(extension, "")
    return name.replace("_", " ").capitalize()


def poll_title(lang: str, rng: random.Random | None = None) -> str:
    """v1: `battle_title_plus` with a random `battle_title_list` suffix for
    `pt`, plain `battle_title` for everything else (`SocialContent.py:368,371`).

    Only the fighter shapes have this; the two-people shape always uses the
    plain title (`:334`), which is why this is not folded into a single
    title helper shared with `battle`'s path A. The `pt` catalog is the only
    one carrying either key — `_catalog_choice`'s `en` fallback would return
    an empty list for any other language, which is exactly why the branch is
    on the language rather than on the key being present.
    """
    if lang != "pt":
        return locales.get("battle_title", lang)
    return locales.get(
        "battle_title_plus", lang, plus=_catalog_choice(lang, "battle_title_list", rng)
    )


def fighter_first(rng: random.Random | None = None) -> bool:
    """v1's coin flip (`SocialContent.py:375`): `if random.choice([0, 1])`,
    the fighter goes first in the media group, the caption and the poll's
    choice list — all three together, never independently."""
    picker = rng.choice if rng is not None else random.choice
    return bool(picker([0, 1]))


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


# ------------------------------------------------------- ONE_TAG / SELF (fighter)


async def _human_side(
    message: Message,
    bot: Bot,
    ctx: ChatContext,
    shape: BattleShape,
    tags: list[str],
) -> tuple[str, str] | None:
    """The person half of a fighter battle: `(display_name, file_id)`, or
    `None` after this function has already replied with v1's failure string
    for whichever shape it was.

    * **ONE_TAG** (`SocialContent.py:345-357`) — v1 scraped
      `telegram.me/{tag}` for that one user and answered `battle_private`
      when the page carried no `<img>`. This port resolves the tag against
      the group roster and asks the Bot API instead (the same redesign the
      two-people shape already shipped, design R2.1); **both** an
      unresolvable tag and a resolved user with no visible photo answer
      `battle_private`, because both are "I could not get that user's
      picture" and v1 has exactly one string for that. The accepted drift is
      the same one recorded for `battle_extract`: a tagged user who has
      never spoken here cannot be resolved, where a scrape sometimes could.
    * **SELF** (`:358-364`) — `getUserProfilePhotos` on the caller, the one
      branch v1 already did through the Bot API, so nothing changes; an
      empty result is v1's `IndexError` path, `battle_no_picture`. The
      display name is `username` when there is one and `first_name`
      otherwise, **without** an `@` (`:359`) — unlike the two-people random
      shape, which prefixes one.
    """
    if shape is BattleShape.ONE_TAG:
        display = tags[0]
        roster = await members.roster(ctx.group_id)
        user_id = _find_in_roster(roster, display)
        if user_id is None:
            await message.reply(t(ctx, "battle_private"))
            return None
        photos = await bot.get_user_profile_photos(user_id, limit=1)
        if not photos.photos:
            await message.reply(t(ctx, "battle_private"))
            return None
        return display, photos.photos[0][-1].file_id

    sender = message.from_user
    if sender is None:  # pragma: no cover - a group message always carries one
        return None
    display = sender.username or sender.first_name
    photos = await bot.get_user_profile_photos(sender.id, limit=1)
    if not photos.photos:
        await message.reply(t(ctx, "battle_no_picture"))
        return None
    return display, photos.photos[0][-1].file_id


async def _fighter_battle(
    message: Message,
    bot: Bot,
    ctx: ChatContext,
    shape: BattleShape,
    tags: list[str],
) -> None:
    """v1's shared tail for the two fighter shapes (`SocialContent.py:366-379`):
    one human against a random image out of the `Fight/` pools, a coin flip
    for who goes first, a media group and a poll.

    Note what this tail does *not* do: there is no `battle_full` flavour
    suffix here. v1 builds that only in the two-people branch (`:335-340`),
    and the caption is a bare `"{a} VS {b}"` — the asymmetry is real and
    preserved.
    """
    human = await _human_side(message, bot, ctx, shape, tags)
    if human is None:
        return
    display, file_id = human

    entry = legacy_assets.choose(fighter_pool_prefix(ctx.lang))
    if entry is None:
        # `legacy-catalog` has not run in this deployment — the same "no
        # bytes seeded yet" state `death.py` degrades on, and the same
        # decision: the user has already seen the reaction and the
        # chat action, and gets nothing more, rather than an exception
        # reaching the dispatcher. v1 had no equivalent: an empty bucket
        # listing crashed in `random.choice`.
        log.warning("battle.fighter_pool_empty", lang=ctx.lang)
        return

    fighter_name = fighter_display_name(entry.source_path)
    data = await storage.store().get(entry.storage_key)
    _, extension = splitext(entry.source_path)
    fighter_file = BufferedInputFile(data, filename=f"fighter{extension}")

    first: str | BufferedInputFile
    second: str | BufferedInputFile
    if fighter_first():
        first, second = fighter_file, file_id
        caption = f"{fighter_name} VS {display}"
        choices = (fighter_name, display)
    else:
        first, second = file_id, fighter_file
        caption = f"{display} VS {fighter_name}"
        choices = (display, fighter_name)

    media: list[MediaUnion] = [
        InputMediaPhoto(media=first, caption=caption),
        InputMediaPhoto(media=second),
    ]
    await message.reply_media_group(media)
    await message.reply_poll(
        poll_title(ctx.lang),
        [InputPollOption(text=choices[0]), InputPollOption(text=choices[1])],
        is_anonymous=False,
        allows_multiple_answers=False,
    )


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
        await _fighter_battle(message, active_bot, ctx, shape, tags)
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
    "fighter_display_name",
    "fighter_first",
    "fighter_pool_prefix",
    "flavour_suffix",
    "parse_tagged_targets",
    "pick_two_random",
    "poll_title",
    "router",
    "uses_random",
]
