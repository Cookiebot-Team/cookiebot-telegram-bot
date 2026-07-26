"""What makes this sandbox *your* bot's sandbox.

Everything that used to be a hardcoded fact about one particular Telegram bot
— its id and username, which users and groups a "seed" creates, which commands
the palette offers, which features a run is meant to prove — lives here, as
data, loaded from one file the bot's own repository owns.

    CB_SANDBOX_CONFIG=./sandbox.config.json   explicit, always wins
    ./sandbox.config.json                     discovered from the working
    ./sandbox.config.toml                     directory and its parents

With no file at all the sandbox still runs: `DEFAULT_CONFIG` is a complete,
bot-agnostic world (a bot, a group, a creator, a member, an anonymous admin,
a DM) that is enough to drive any Telegram bot by hand. A config file replaces
the parts you care about and inherits the rest.

The four things a bot repository actually customises:

`bot`       the identity `getMe` returns. A gateway that resolves its own
            username at startup and then filters `/cmd@username` will not
            match a single command unless this agrees with the token it was
            given.
`seeds`     named starting worlds. One entry per situation worth reaching in
            one click — "a group with an anonymous admin", "a user who has
            never pressed Start", "a known-bad account that has not joined
            yet".
`features`  what the bot *does*, as a list a person can validate against. This
            is the axis the web client groups scenarios by: pick a feature,
            see every check that ran against it and how each one ended.
`commands`  the palette. Generated from the bot's own parser, ideally, so a
            newly added alias appears without anyone retyping it.

Nothing here reaches into a store or a request — `control_api.py` applies a
`SeedFixture` and serves a `FeatureSpec`; this module only knows what they are.
"""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from cb_sandbox.logging import get_logger

log = get_logger("cb.sandbox.config")

#: Explicit path to a config file. Beats discovery, and is what a test harness
#: or a `docker run` should set — discovery depends on the working directory,
#: which a subprocess does not always inherit the way its author assumed.
CONFIG_PATH_ENV = "CB_SANDBOX_CONFIG"

#: Filenames discovery looks for, in order, in the working directory and each
#: of its parents.
CONFIG_FILENAMES: tuple[str, ...] = (
    "sandbox.config.json",
    "sandbox.config.toml",
    ".sandbox.json",
)

#: How far up from the working directory discovery walks. Enough to find a
#: repository root from a subdirectory, bounded so a sandbox started in `/tmp`
#: does not silently adopt a config file from someone's home directory.
_DISCOVERY_DEPTH = 4

#: Env overrides applied after the file loads, for the handful of fields a
#: process launcher needs to vary per run without writing a second file.
DB_PATH_ENV = "CB_SANDBOX_DB"
BOT_ID_ENV = "CB_SANDBOX_BOT_ID"
BOT_USERNAME_ENV = "CB_SANDBOX_BOT_USERNAME"
BOT_FIRST_NAME_ENV = "CB_SANDBOX_BOT_FIRST_NAME"


# --------------------------------------------------------------------- shapes


@dataclass(frozen=True, slots=True)
class BotIdentity:
    """`getMe`'s answer, and the `from` on every message the bot sends.

    `id` must match the numeric prefix of the token the bot is configured
    with, because that prefix is what most client libraries derive `bot.id`
    from without ever calling `getMe` — a mismatch makes "did the bot send
    this?" answer differently on the two sides of the same message.
    """

    id: int = 424242
    username: str = "sandbox_bot"
    first_name: str = "Sandbox Bot"
    #: Real `getMe` fields a handler may branch on. The defaults describe a
    #: group-moderation bot (privacy mode off, so it sees every message),
    #: which is the shape this tool is most useful for; a bot that only reads
    #: commands should set `can_read_all_group_messages` to false so the
    #: sandbox tells it the same thing BotFather would.
    can_join_groups: bool = True
    can_read_all_group_messages: bool = True
    supports_inline_queries: bool = False


@dataclass(frozen=True, slots=True)
class SeedUser:
    """One account a seed creates. `key` is how the rest of the config refers
    to it (a chat's member list, a preset's acting user); it never reaches
    Telegram. Ids are minted by the store at seed time, so nothing here can
    collide with a user created later by hand."""

    key: str
    first_name: str
    username: str
    last_name: str | None = None
    language_code: str = "en"


@dataclass(frozen=True, slots=True)
class SeedMember:
    user: str
    role: str = "member"
    #: The admin's own "remain anonymous" toggle. Worth seeding rather than
    #: toggling by hand: it is the state most bots get wrong, because an
    #: anonymous admin's message arrives `from` GroupAnonymousBot with the
    #: group in `sender_chat`, and an admin check written against `from` fails
    #: for exactly the person most entitled to pass it.
    anonymous: bool = False


@dataclass(frozen=True, slots=True)
class SeedChat:
    key: str
    title: str
    type: str = "supergroup"
    #: The bot's own membership. `None` leaves the bot out entirely — the
    #: situation worth testing on purpose, since most moderation calls fail
    #: with a permissions error rather than doing nothing.
    bot_role: str | None = "administrator"
    members: tuple[SeedMember, ...] = ()


@dataclass(frozen=True, slots=True)
class SeedFixture:
    """A named starting world. `POST /api/seed` wipes everything and applies
    one of these; `POST /api/reset` applies `SandboxConfig.default_seed`."""

    name: str
    title: str = ""
    description: str = ""
    users: tuple[SeedUser, ...] = ()
    chats: tuple[SeedChat, ...] = ()
    #: Users who have "pressed Start", i.e. have a private chat the bot is
    #: allowed to write into. Telegram forbids a bot from opening one, so a
    #: feature that answers privately cannot be exercised at all without this.
    dms: tuple[str, ...] = ()

    def label(self) -> str:
        return self.title or self.name


@dataclass(frozen=True, slots=True)
class FeatureSpec:
    """One thing the bot does, as a validator would name it.

    The web client groups scenarios under these: pick a feature, see every
    scenario that exercised it and how each ended. That grouping is why `tags`
    exists — a test suite that already labels its runs ("captcha", "rules")
    gets feature grouping without changing a line, because a scenario whose
    tags match is claimed by this feature.
    """

    id: str
    title: str
    description: str | None = None
    #: Free-form; "done" / "partial" / "planned" / "blocked" get a distinct
    #: treatment in the web client and anything else renders verbatim. A
    #: feature the bot has not built yet is worth listing: it turns "the bot
    #: ignored me" from a bug report into an expected result.
    status: str = "unknown"
    commands: tuple[str, ...] = ()
    #: Scenario tags that mean "this feature", for suites that tag rather than
    #: setting `feature` explicitly. Matched case-insensitively; `id` and
    #: `title` always match themselves, so listing them again is unnecessary.
    tags: tuple[str, ...] = ()
    docs: str | None = None


@dataclass(frozen=True, slots=True)
class CommandSpec:
    """One row in the command palette. `primary` is what a click sends."""

    primary: str
    canonical: str = ""
    aliases: tuple[str, ...] = ()
    feature_id: str | None = None
    title: str | None = None
    status: str = "unknown"
    hint: str | None = None

    def key(self) -> str:
        return self.canonical or self.primary.lstrip("/")


@dataclass(frozen=True, slots=True)
class PresetSpec:
    """One click that puts the tester in front of a specific question.

    A preset seeds a world, picks who to act as, and states what to do and
    what to watch for. It deliberately does *not* assert the outcome: the
    bot's real reaction is the result, and a preset that graded itself would
    be testing its own expectations rather than the bot.
    """

    id: str
    button: str
    seed: str = "default"
    label: str | None = None
    feature_id: str | None = None
    #: A `SeedUser.key` (or a bare username) from the seed this preset loads.
    acting_user: str | None = None
    #: `{"first_name": ..., "username_prefix": ...}` — mint a fresh account
    #: and act as it, for the "brand new member" class of check, where the
    #: point is that the account has no history.
    create_user: dict[str, Any] | None = None
    chat: str | None = None
    what_to_do: str = ""
    what_to_look_for: str = ""

    def title(self) -> str:
        return self.label or self.button


@dataclass(frozen=True, slots=True)
class SandboxConfig:
    bot: BotIdentity = field(default_factory=BotIdentity)
    db_path: str = "sandbox.duckdb"
    default_seed: str = "default"
    seeds: tuple[SeedFixture, ...] = ()
    features: tuple[FeatureSpec, ...] = ()
    commands: tuple[CommandSpec, ...] = ()
    presets: tuple[PresetSpec, ...] = ()
    #: Where this came from, surfaced by `GET /api/kit` so a tester looking at
    #: a palette that disagrees with the bot knows which file to fix.
    source_path: str | None = None

    def seed(self, name: str) -> SeedFixture | None:
        return next((fixture for fixture in self.seeds if fixture.name == name), None)

    def seed_names(self) -> list[str]:
        return [fixture.name for fixture in self.seeds]

    def feature(self, feature_id: str) -> FeatureSpec | None:
        return next((spec for spec in self.features if spec.id == feature_id), None)

    def feature_for_tags(self, tags: list[str] | tuple[str, ...]) -> str | None:
        """Which feature a scenario belongs to, inferred from its tags.

        The fallback for a test suite that labels its runs but has never heard
        of this config's `feature` field — which is every suite that predates
        it. A feature claims a scenario if the scenario carries its id, its
        title, or any of its declared `tags`. First match in declaration
        order wins: two features claiming the same tag is a config bug, and
        picking deterministically makes it visible rather than flickering.
        """
        lowered = {tag.lower() for tag in tags}
        for spec in self.features:
            candidates = {spec.id.lower(), spec.title.lower(), *(t.lower() for t in spec.tags)}
            if lowered & candidates:
                return spec.id
        return None


# ------------------------------------------------------------------ defaults


def _default_seeds() -> tuple[SeedFixture, ...]:
    """Three worlds that are useful for any Telegram bot, named after the
    situation rather than after any one bot's features."""
    alice = SeedUser(key="alice", first_name="Alice", username="alice")
    bob = SeedUser(key="bob", first_name="Bob", username="bob")
    carol = SeedUser(key="carol", first_name="Carol", username="carol")
    dana = SeedUser(key="dana", first_name="Dana", username="dana")
    return (
        SeedFixture(
            name="default",
            title="Group with an anonymous admin",
            description=(
                "A supergroup with the bot as administrator, a creator, a plain member, "
                "and an admin who posts anonymously. Enough to drive almost any "
                "group-facing command, including the admin checks most bots get wrong."
            ),
            users=(alice, bob, carol),
            chats=(
                SeedChat(
                    key="main",
                    title="Sandbox Group",
                    bot_role="administrator",
                    members=(
                        SeedMember(user="alice", role="creator"),
                        SeedMember(user="bob", role="member"),
                        SeedMember(user="carol", role="administrator", anonymous=True),
                    ),
                ),
            ),
        ),
        SeedFixture(
            name="empty",
            title="Empty",
            description="Nothing at all — build the world by hand from here.",
        ),
        SeedFixture(
            name="dm",
            title="Private chat",
            description=(
                "The bot and one user in a private chat, for commands that only make "
                "sense outside a group. The DM exists, which stands for the user having "
                "pressed Start — without it a bot may not write to them at all."
            ),
            users=(dana,),
            dms=("dana",),
        ),
    )


def _default_presets() -> tuple[PresetSpec, ...]:
    """The one check every Telegram bot with an admin surface should survive,
    plus a blank start. A bot's own config replaces these with its own."""
    return (
        PresetSpec(
            id="anonymous-admin",
            button="Anonymous admin sends a command",
            seed="default",
            acting_user="carol",
            what_to_do="Acting as Carol, send an admin-only command.",
            what_to_look_for=(
                "It should be accepted. An anonymous admin's message arrives from "
                "GroupAnonymousBot with the group in sender_chat, so a bot that checks "
                "`from` alone rejects the one person most entitled to pass."
            ),
        ),
    )


#: A complete, bot-agnostic sandbox — what runs when no config file is found.
DEFAULT_CONFIG = SandboxConfig(
    bot=BotIdentity(),
    db_path="sandbox.duckdb",
    default_seed="default",
    seeds=_default_seeds(),
    features=(),
    commands=(),
    presets=_default_presets(),
    source_path=None,
)


# ------------------------------------------------------------------- parsing


def _str_tuple(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, list | tuple):
        return ()
    return tuple(str(item) for item in raw)


def _seed_user(raw: dict[str, Any]) -> SeedUser:
    # `key` falls back to `username` because a config that names its users
    # once, by username, is the common shape — requiring a second identical
    # field would be ceremony with no information in it.
    username = str(raw.get("username") or raw["key"])
    return SeedUser(
        key=str(raw.get("key") or username),
        first_name=str(raw.get("first_name") or username),
        username=username,
        last_name=raw.get("last_name"),
        language_code=str(raw.get("language_code", "en")),
    )


def _seed_chat(raw: dict[str, Any], index: int) -> SeedChat:
    members = tuple(
        SeedMember(
            user=str(member["user"]),
            role=str(member.get("role", "member")),
            anonymous=bool(member.get("anonymous", False)),
        )
        for member in raw.get("members", [])
    )
    # `bot_role` distinguishes three states, so a missing key and an explicit
    # null must not collapse into the same thing: absent means "administrator"
    # (what a seed almost always wants), null means "the bot is not in this
    # chat" (a situation worth reaching deliberately).
    bot_role = raw.get("bot_role", "administrator")
    return SeedChat(
        key=str(raw.get("key") or f"chat{index}"),
        title=str(raw.get("title") or f"Sandbox chat {index}"),
        type=str(raw.get("type", "supergroup")),
        bot_role=str(bot_role) if bot_role is not None else None,
        members=members,
    )


def _seed_fixture(raw: dict[str, Any]) -> SeedFixture:
    return SeedFixture(
        name=str(raw["name"]),
        title=str(raw.get("title", "")),
        description=str(raw.get("description", "")),
        users=tuple(_seed_user(user) for user in raw.get("users", [])),
        chats=tuple(_seed_chat(chat, i) for i, chat in enumerate(raw.get("chats", []))),
        dms=_str_tuple(raw.get("dms")),
    )


def _feature(raw: dict[str, Any]) -> FeatureSpec:
    return FeatureSpec(
        id=str(raw["id"]),
        title=str(raw.get("title") or raw["id"]),
        description=raw.get("description"),
        status=str(raw.get("status", "unknown")),
        commands=_str_tuple(raw.get("commands")),
        tags=_str_tuple(raw.get("tags")),
        docs=raw.get("docs"),
    )


def _command(raw: dict[str, Any]) -> CommandSpec:
    primary = str(raw["primary"])
    return CommandSpec(
        primary=primary,
        canonical=str(raw.get("canonical") or primary.lstrip("/")),
        aliases=_str_tuple(raw.get("aliases")),
        feature_id=raw.get("feature_id"),
        title=raw.get("title"),
        status=str(raw.get("status", "unknown")),
        hint=raw.get("hint"),
    )


def _preset(raw: dict[str, Any]) -> PresetSpec:
    return PresetSpec(
        id=str(raw["id"]),
        button=str(raw.get("button") or raw["id"]),
        seed=str(raw.get("seed", "default")),
        label=raw.get("label"),
        feature_id=raw.get("feature_id"),
        acting_user=raw.get("acting_user"),
        create_user=raw.get("create_user"),
        chat=raw.get("chat"),
        what_to_do=str(raw.get("what_to_do", "")),
        what_to_look_for=str(raw.get("what_to_look_for", "")),
    )


def from_dict(raw: dict[str, Any], *, source_path: str | None = None) -> SandboxConfig:
    """Build a config from an already-parsed mapping.

    Every section is optional and falls back to `DEFAULT_CONFIG`'s — a file
    that only overrides `bot` keeps the default seeds, and a file that only
    lists `features` keeps the default bot. `seeds` is the one exception worth
    naming: providing it *replaces* the built-in three rather than merging,
    because a bot whose world looks nothing like the default one should not
    have to carry three fixtures it will never press.
    """
    bot_raw = raw.get("bot", {})
    bot = BotIdentity(
        id=int(bot_raw.get("id", DEFAULT_CONFIG.bot.id)),
        username=str(bot_raw.get("username", DEFAULT_CONFIG.bot.username)),
        first_name=str(bot_raw.get("first_name", DEFAULT_CONFIG.bot.first_name)),
        can_join_groups=bool(bot_raw.get("can_join_groups", DEFAULT_CONFIG.bot.can_join_groups)),
        can_read_all_group_messages=bool(
            bot_raw.get(
                "can_read_all_group_messages", DEFAULT_CONFIG.bot.can_read_all_group_messages
            )
        ),
        supports_inline_queries=bool(
            bot_raw.get("supports_inline_queries", DEFAULT_CONFIG.bot.supports_inline_queries)
        ),
    )

    seeds = (
        tuple(_seed_fixture(seed) for seed in raw["seeds"])
        if raw.get("seeds")
        else DEFAULT_CONFIG.seeds
    )
    presets = (
        tuple(_preset(preset) for preset in raw["presets"])
        if raw.get("presets")
        else DEFAULT_CONFIG.presets
    )
    default_seed = str(raw.get("default_seed", DEFAULT_CONFIG.default_seed))
    if all(seed.name != default_seed for seed in seeds):
        # Not fatal: a reset that 400s because of a typo three levels deep in
        # a config file is a worse failure than one that resets to something.
        log.warning(
            "sandbox.config.unknown_default_seed",
            default_seed=default_seed,
            known=[seed.name for seed in seeds],
        )
        default_seed = seeds[0].name if seeds else DEFAULT_CONFIG.default_seed

    return SandboxConfig(
        bot=bot,
        db_path=str(raw.get("db", raw.get("db_path", DEFAULT_CONFIG.db_path))),
        default_seed=default_seed,
        seeds=seeds,
        features=tuple(_feature(feature) for feature in raw.get("features", [])),
        commands=tuple(_command(command) for command in raw.get("commands", [])),
        presets=presets,
        source_path=source_path,
    )


def _read_file(path: Path) -> dict[str, Any]:
    if path.suffix == ".toml":
        return tomllib.loads(path.read_text())
    parsed = json.loads(path.read_text())
    if not isinstance(parsed, dict):
        raise ValueError("a sandbox config file must contain a top-level object")
    return parsed


def discover_config_path(start: Path | None = None) -> Path | None:
    """The config file for this run, or `None` if there isn't one.

    `CB_SANDBOX_CONFIG` wins outright, and an explicit path that does not
    exist is an error rather than a silent fall-through to discovery: a
    launcher that names a file has stated an intent, and quietly running with
    different data than it asked for is the kind of failure that gets
    diagnosed as "the bot is broken".
    """
    explicit = os.environ.get(CONFIG_PATH_ENV)
    if explicit:
        path = Path(explicit).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"{CONFIG_PATH_ENV}={explicit} does not exist")
        return path

    here = (start or Path.cwd()).resolve()
    for directory in (here, *here.parents[: _DISCOVERY_DEPTH - 1]):
        for filename in CONFIG_FILENAMES:
            candidate = directory / filename
            if candidate.is_file():
                return candidate
    return None


def load_config(path: Path | str | None = None) -> SandboxConfig:
    """Load a config, apply the environment overrides, and return it.

    A malformed file degrades to the defaults with a warning rather than
    refusing to start. The sandbox's whole value is being a thing you can put
    in front of a bot in one command; a config typo that turns that into a
    stack trace teaches the person to stop using the tool, not to fix the typo.
    """
    resolved = Path(path) if path is not None else discover_config_path()
    config = DEFAULT_CONFIG
    if resolved is not None:
        try:
            config = from_dict(_read_file(resolved), source_path=str(resolved))
        except Exception as exc:  # noqa: BLE001 - a bad config must degrade, not crash
            log.warning("sandbox.config.load_failed", path=str(resolved), error=str(exc))
            config = replace(DEFAULT_CONFIG, source_path=None)

    return _apply_env_overrides(config)


def _apply_env_overrides(config: SandboxConfig) -> SandboxConfig:
    bot = config.bot
    raw_id = os.environ.get(BOT_ID_ENV)
    if raw_id:
        try:
            bot = replace(bot, id=int(raw_id))
        except ValueError:
            log.warning("sandbox.config.bad_bot_id", value=raw_id)
    if os.environ.get(BOT_USERNAME_ENV):
        bot = replace(bot, username=os.environ[BOT_USERNAME_ENV])
    if os.environ.get(BOT_FIRST_NAME_ENV):
        bot = replace(bot, first_name=os.environ[BOT_FIRST_NAME_ENV])

    db_path = os.environ.get(DB_PATH_ENV) or config.db_path
    return replace(config, bot=bot, db_path=db_path)


# ---------------------------------------------------------------- the process


_config: SandboxConfig | None = None


def get_config() -> SandboxConfig:
    """The config this process is running with, loaded once on first use."""
    global _config
    if _config is None:
        _config = load_config()
        log.info(
            "sandbox.config.loaded",
            source=_config.source_path or "built-in defaults",
            bot=_config.bot.username,
            seeds=_config.seed_names(),
            features=len(_config.features),
        )
    return _config


def set_config(config: SandboxConfig) -> None:
    """Replace the process config — for tests, and for a host application that
    builds its config in code rather than in a file."""
    global _config
    _config = config


def reset_config() -> None:
    """Forget the loaded config so the next `get_config()` re-reads the
    environment. Paired with `set_config` in test teardown."""
    global _config
    _config = None


def bot() -> BotIdentity:
    """Shorthand for the identity, read on every outbound message."""
    return get_config().bot
