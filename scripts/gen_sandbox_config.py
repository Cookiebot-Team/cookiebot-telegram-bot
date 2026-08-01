"""Generate `sandbox.config.json` — what makes the bot-agnostic sandbox
(the `telegram-sandbox` tool) into *Cookiebot's* sandbox.

The sandbox itself knows nothing about this bot. Identity, seed worlds,
features and the command palette are all data it loads from one file, and this
script is what writes that file — from the two places that already know the
truth, so it cannot drift the moment someone adds an alias or ports a feature:

  cb_core.textmatch.COMMAND_ALIASES   every trigger word the parser accepts,
                                       grouped by canonical command
  scripts.spec.FEATURES               each feature's id, title, triggers and
                                       port status, so the palette can show
                                       "not implemented yet — expect silence"
                                       instead of a tester filing a false bug
                                       against a command that was never
                                       finished, and so the web client can
                                       group a whole test run by feature

Re-run after any change to either source:

    ./.venv/bin/python scripts/gen_sandbox_config.py

Not wired into `cb.py`: it writes a checked-in file, and a generator that runs
inside the test gate turns "the spec changed" into "the tests are failing",
which is a worse signal than a one-line diff in review.

The hand-written parts — the seed worlds and the presets — live in this file
too, at the bottom. They are the only Cookiebot-specific *behaviour* left
anywhere near the sandbox, and they are declarative.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "packages" / "cb-core" / "src"))

from scripts.spec import FEATURES  # noqa: E402

from cb_core.textmatch import COMMAND_ALIASES  # noqa: E402

OUT_PATH = ROOT / "sandbox.config.json"

#: `getMe`'s answer. The id must match the numeric prefix of the token the
#: gateway is started with (`CB_BOT_TOKENS='{"cookiebot": "424242:SANDBOX"}'`)
#: — aiogram derives `bot.id` from that prefix without calling `getMe`, so a
#: mismatch makes "did *I* send this message" answer differently on the two
#: sides of the same message.
BOT = {
    "id": 424242,
    "username": "CookieMWbot",
    "first_name": "Cookiebot",
    # Privacy mode is off in production: the moderation and anti-spam features
    # need every group message, not just commands addressed at the bot.
    "can_join_groups": True,
    "can_read_all_group_messages": True,
    "supports_inline_queries": False,
}

#: A handful of internal canonical names are not real command text a user can
#: type (the six partnered-convention commands share the `con_*` family name
#: from `docs/site/content/docs/feature-map.mdx`, not a `/con_bff` trigger) — pick the trigger a
#: human would actually send instead of leaking the internal name.
PRIMARY_OVERRIDE: dict[str, str] = {
    "con_bff": "/bff",
    "con_patas": "/patas",
    "con_fursmeet": "/fursmeet",
    "con_trex": "/trex",
    "con_furcamp": "/furcamp",
    "con_pawstral": "/pawstral",
}

#: Short, tester-facing hints for the commands where "what does this do" isn't
#: obvious from the name alone, or where the interesting case is a specific
#: actor/state rather than the command itself.
HINTS: dict[str, str] = {
    "config": "Opens the admin config menu — send it as an anonymous admin to check the v1 defect the port fixed.",
    "newrules": "Admin only. Try it as a plain member (should refuse) and as an anonymous admin (should work).",
    "newwelcome": "Admin only, same anonymous-admin check as /newrules.",
    "dice": "Bare /dice/ /dado rolls a d6; /d<N> (e.g. /d20, /d100) rolls an N-sided die with no upper bound.",
    "calladms": "Pings every admin in the chat — compare the reply as a member vs. as an admin.",
    "config_setlang": "Language is set from this menu; /rules and /regras should then answer in that language.",
}

#: Areas from `scripts/spec.py` that describe infrastructure rather than
#: something a person can drive from a chat window. Listing them as features
#: in the sandbox would put rows in the validation view that no scenario can
#: ever fill, which reads as "untested" when the truth is "not testable here".
NON_INTERACTIVE_AREAS = {"platform"}


def _commands() -> list[dict[str, Any]]:
    by_canonical: dict[str, set[str]] = {}
    for alias, canonical in COMMAND_ALIASES.items():
        by_canonical.setdefault(canonical, set()).add(f"/{alias}")

    # canonical trigger text (bare, lowercase) -> feature id, so a group of
    # aliases picks up the one feature row that actually ships it.
    feature_by_trigger: dict[str, tuple[str, str, str]] = {}
    for feature in FEATURES:
        for trigger in feature.triggers:
            feature_by_trigger[trigger.lstrip("/").lower()] = (
                feature.id,
                feature.title,
                feature.status.value,
            )

    commands: list[dict[str, Any]] = []
    for canonical, aliases in sorted(by_canonical.items()):
        aliases_sorted = sorted(aliases)

        feature = None
        for alias in aliases_sorted:
            bare = alias.lstrip("/")
            if bare in feature_by_trigger:
                feature = feature_by_trigger[bare]
                break

        # Prefer, in order: the explicit override (for canonical names that are
        # not real command text); the alias spelled exactly like the canonical
        # name (the common case — "rules", "dice", "config" are all both); the
        # alias a ported feature's own trigger list leads with; whatever sorts
        # first, so the choice is at least deterministic.
        primary = PRIMARY_OVERRIDE.get(canonical)
        if primary is None and f"/{canonical}" in aliases_sorted:
            primary = f"/{canonical}"
        if primary is None and feature is not None:
            for trigger in (t for t in aliases_sorted if t.lstrip("/") in feature_by_trigger):
                primary = trigger
                break
        if primary is None:
            primary = aliases_sorted[0]

        hint = HINTS.get(canonical)
        # The /d<N> shorthand (`_DICE_SHORTHAND` in textmatch.py) is parsed by
        # regex, not looked up in COMMAND_ALIASES, so it has no row of its own —
        # note it on `dice` rather than fabricating a fake command string.
        if canonical == "dice" and hint is None:
            hint = HINTS["dice"]

        commands.append(
            {
                "canonical": canonical,
                "primary": primary,
                "aliases": [a for a in aliases_sorted if a != primary],
                "feature_id": feature[0] if feature else None,
                "title": feature[1] if feature else None,
                "status": feature[2] if feature else "unknown",
                "hint": hint,
            }
        )
    return commands


#: Extra scenario tags that mean a given feature. The acceptance suite's
#: modules are already named after feature ids (`qa/test_core_rules.py` tags
#: its scenarios `core_rules`, which matches by id), but the e2e suite's are
#: named after the *situation* they drive, so those need saying out loud.
#: A tag must name exactly one feature — `build()` asserts that, because a tag
#: claimed by two features would file scenarios under whichever happened to be
#: declared first, which is a silently wrong answer to "was this checked".
EXTRA_FEATURE_TAGS: dict[str, list[str]] = {
    "core_groupguardian": ["captcha", "join_chain"],
    "util_config": ["config_menu"],
    "core_privacy": ["privacy_and_commands"],
}


def _features() -> list[dict[str, Any]]:
    """Every user-drivable feature, as the sandbox's validation view needs it.

    `tags` is what makes an existing suite group correctly without being
    rewritten: the test kit tags each scenario with its module name minus
    `test_`, and the sandbox files a scenario under the first feature whose
    id, title or tags match. The area (`core`, `util`) is deliberately *not*
    a tag — it names a dozen features at once, so a scenario carrying it would
    be filed under whichever one sorted first.
    """
    features: list[dict[str, Any]] = []
    for feature in FEATURES:
        if feature.area in NON_INTERACTIVE_AREAS:
            continue
        short = feature.id.split("_", 1)[-1]
        tags = sorted({short, *EXTRA_FEATURE_TAGS.get(feature.id, ())})
        features.append(
            {
                "id": feature.id,
                "title": feature.title,
                "description": feature.notes or None,
                "status": feature.status.value,
                "commands": list(feature.triggers),
                "tags": tags,
                "docs": f"docs/contracts/{feature.id}.md"
                if (ROOT / "docs" / "contracts" / f"{feature.id}.md").exists()
                else None,
            }
        )
    return features


#: The starting worlds. `default`/`empty`/`dm` mirror what the sandbox ships
#: with; `doomlist` is Cookiebot's own, and is the reason seeds are data:
#: reaching "a flagged account that has not joined yet" used to require a
#: hand-written seed function inside the sandbox package.
SEEDS: list[dict[str, Any]] = [
    {
        "name": "default",
        "title": "Group with an anonymous admin",
        "description": (
            "A supergroup with the bot as administrator, a creator, a plain member, and "
            "an admin who posts anonymously. Several features (mediarestrict, config, "
            "calladms) check whether the bot is an admin before acting, so seeding the "
            "bot's own membership is not decorative."
        ),
        "users": [
            {"key": "alice", "first_name": "Alice", "username": "alice"},
            {"key": "bob", "first_name": "Bob", "username": "bob"},
            {"key": "carol", "first_name": "Carol", "username": "carol"},
        ],
        "chats": [
            {
                "key": "main",
                "title": "Cookiebot Sandbox Group",
                "type": "supergroup",
                "bot_role": "administrator",
                "members": [
                    {"user": "alice", "role": "creator"},
                    {"user": "bob", "role": "member"},
                    {"user": "carol", "role": "administrator", "anonymous": True},
                ],
            }
        ],
    },
    {
        "name": "empty",
        "title": "Empty",
        "description": "A blank slate — everything the caller builds by hand from here.",
    },
    {
        "name": "dm",
        "title": "Private chat",
        "description": (
            "The bot and one user in a private chat, for the commands that only make "
            "sense outside a group. The DM existing stands for the user having pressed "
            "Start: Telegram forbids a bot from opening one."
        ),
        "users": [{"key": "dana", "first_name": "Dana", "username": "dana"}],
        "dms": ["dana"],
    },
    {
        "name": "doomlist",
        "title": "Doomlisted account, not yet joined",
        "description": (
            "The default world plus a raider account whose name carries one of the "
            "glyphs check_local_blacklist matches "
            "(cb_gateway/handlers/doomlist.py:_FORBIDDEN_NAME_CHARS). That check runs "
            "entirely off User.full_name against a fixed table — no network call, no "
            "seeded Postgres row — so it fires deterministically the moment this "
            "account self-joins, unlike the cas.chat/burrbot branches of the same "
            "handler, which depend on a vendor's live opinion of a sandbox-only id. "
            "The account is deliberately in no chat: pressing join is the whole test."
        ),
        "users": [
            {"key": "alice", "first_name": "Alice", "username": "alice"},
            {"key": "bob", "first_name": "Bob", "username": "bob"},
            {"key": "carol", "first_name": "Carol", "username": "carol"},
            {"key": "raider", "first_name": "卐Raider", "username": "raider_incoming"},
        ],
        "chats": [
            {
                "key": "main",
                "title": "Cookiebot Sandbox Group",
                "type": "supergroup",
                "bot_role": "administrator",
                "members": [
                    {"user": "alice", "role": "creator"},
                    {"user": "bob", "role": "member"},
                    {"user": "carol", "role": "administrator", "anonymous": True},
                ],
            }
        ],
    },
]

#: One click each for the checks the sandbox README's "what it is for" table
#: lists. Every preset seeds a world, picks who to act as, and states what to
#: do and what to watch for — it does not assert the outcome, because the
#: bot's real reaction *is* the result and faking that would defeat the tool.
PRESETS: list[dict[str, Any]] = [
    {
        "id": "anon-admin",
        "button": "Anonymous admin -> /config",
        "label": "Anonymous admin bypass",
        "seed": "default",
        "feature_id": "util_config",
        "acting_user": "carol",
        "what_to_do": "Acting as Carol, send /config.",
        "what_to_look_for": (
            "The config menu (inline buttons) should open. v1 rejected this and told "
            "the admin to turn anonymity off first — that's the defect this port fixed."
        ),
    },
    {
        "id": "sticker-spam",
        "button": "Sticker spam",
        "label": "Sticker spam",
        "seed": "default",
        "feature_id": "core_stickerspam",
        "acting_user": "bob",
        "what_to_do": (
            "Acting as Bob, pick Sticker in the composer and send with a high repeat "
            "count (try x6 or more)."
        ),
        "what_to_look_for": (
            "Once the group's flood threshold is crossed, deleteMessage calls should "
            "start appearing in the API log for the extra stickers."
        ),
    },
    {
        "id": "newcomer-media",
        "button": "Newcomer media restriction",
        "label": "Newcomer media restriction",
        "seed": "default",
        "feature_id": "core_mediarestrict",
        "create_user": {"first_name": "Newcomer", "username_prefix": "newcomer"},
        "what_to_do": "Click '+ Join as me' under Members, then immediately send a photo.",
        "what_to_look_for": (
            "A brand-new member's media should be restricted (deleted or the account "
            "muted) until they're established — check the API log for restrictChatMember "
            "or deleteMessage."
        ),
    },
    {
        "id": "doomlist",
        "button": "Doomlisted join",
        "label": "Doomlisted join",
        "seed": "doomlist",
        "feature_id": "util_doomlist",
        "acting_user": "raider",
        "what_to_do": "Click '+ Join as me' under Members for this account.",
        "what_to_look_for": (
            "banChatMember should appear in the API log immediately and the join should "
            "never settle into a normal membership — the name carries a glyph the local "
            "blacklist check matches."
        ),
    },
]


def _assert_tags_are_unambiguous(features: list[dict[str, Any]]) -> None:
    """A tag (or an id, or a title) that two features answer to makes scenario
    grouping order-dependent, and the wrong answer looks exactly like the
    right one — a feature quietly showing zero scenarios while another shows
    twice as many. Cheaper to fail the generator."""
    owners: dict[str, list[str]] = {}
    for feature in features:
        # Lowercased *before* the set: matching is case-insensitive, so a
        # feature whose title and short tag differ only in case ("Giveaways"
        # / "giveaways") is one token, not a feature clashing with itself.
        tokens = {token.lower() for token in (feature["id"], feature["title"], *feature["tags"])}
        for token in tokens:
            owners.setdefault(token, []).append(feature["id"])
    clashes = {token: ids for token, ids in owners.items() if len(ids) > 1}
    if clashes:
        rendered = "; ".join(f"{token!r} -> {ids}" for token, ids in sorted(clashes.items()))
        raise SystemExit(f"ambiguous feature tags: {rendered}")


def build() -> dict[str, Any]:
    features = _features()
    _assert_tags_are_unambiguous(features)
    return {
        "bot": BOT,
        "db": "sandbox.duckdb",
        "default_seed": "default",
        "seeds": SEEDS,
        "presets": PRESETS,
        "features": features,
        "commands": _commands(),
    }


if __name__ == "__main__":
    config = build()
    OUT_PATH.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n")
    print(
        f"wrote {OUT_PATH.relative_to(ROOT)}: "
        f"{len(config['features'])} features, {len(config['commands'])} commands, "
        f"{len(config['seeds'])} seeds, {len(config['presets'])} presets"
    )
