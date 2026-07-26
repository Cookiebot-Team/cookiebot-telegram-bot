"""What makes the sandbox belong to one particular bot, and nothing else.

These tests exist because the failure they guard against is silent. A sandbox
that quietly falls back to its own defaults — the wrong bot username, the wrong
seed, no features — still starts, still answers, and still lets a tester click
around; it just validates a bot that isn't the one under test. Every assertion
here is about the config actually reaching the surfaces that use it.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from cb_sandbox.config import (
    DEFAULT_CONFIG,
    BotIdentity,
    FeatureSpec,
    SandboxConfig,
    SeedFixture,
    discover_config_path,
    from_dict,
    load_config,
)


class TestDefaults:
    def test_a_sandbox_with_no_config_still_has_a_usable_world(self) -> None:
        """The zero-configuration path is the one a new user meets first: run
        the server, open the client, drive a bot. If that needs a config file,
        the tool has a cliff at the exact moment someone is deciding whether
        to keep using it."""
        assert DEFAULT_CONFIG.seed("default") is not None
        assert DEFAULT_CONFIG.seed("empty") is not None
        assert DEFAULT_CONFIG.seed("dm") is not None
        assert DEFAULT_CONFIG.default_seed in DEFAULT_CONFIG.seed_names()

    def test_the_default_group_has_an_anonymous_admin(self) -> None:
        """The one seeded state worth shipping by default for any Telegram
        bot: an anonymous admin is the case a `from`-based admin check gets
        wrong, in every bot that has ever written one."""
        default = DEFAULT_CONFIG.seed("default")
        assert default is not None
        members = default.chats[0].members
        assert any(m.anonymous and m.role == "administrator" for m in members)

    def test_the_dm_seed_opens_a_private_chat(self) -> None:
        dm = DEFAULT_CONFIG.seed("dm")
        assert dm is not None
        assert dm.dms == ("dana",)


class TestParsing:
    def test_a_partial_config_inherits_the_rest(self) -> None:
        """A file that only names the bot should not silently lose the seeds:
        that is the difference between "configure what you care about" and "an
        all-or-nothing file nobody wants to write"."""
        config = from_dict({"bot": {"username": "mybot", "id": 7}})
        assert config.bot.username == "mybot"
        assert config.bot.id == 7
        assert config.seed_names() == DEFAULT_CONFIG.seed_names()

    def test_providing_seeds_replaces_rather_than_merges(self) -> None:
        config = from_dict({"seeds": [{"name": "only"}], "default_seed": "only"})
        assert config.seed_names() == ["only"]

    def test_a_default_seed_that_does_not_exist_falls_back_instead_of_crashing(self) -> None:
        """A typo three levels into a config file should not make `POST
        /api/reset` 400 forever — a reset that resets to *something* is
        recoverable, one that always errors is not."""
        config = from_dict({"seeds": [{"name": "real"}], "default_seed": "typo"})
        assert config.default_seed == "real"

    def test_bot_role_null_means_the_bot_is_not_in_the_chat(self) -> None:
        """Absent and explicit-null must not collapse: "the bot is not a
        member" is a situation worth reaching deliberately, because most
        moderation calls fail with a permissions error rather than doing
        nothing."""
        with_bot = from_dict({"seeds": [{"name": "s", "chats": [{"key": "c", "title": "C"}]}]})
        without = from_dict(
            {"seeds": [{"name": "s", "chats": [{"key": "c", "title": "C", "bot_role": None}]}]}
        )
        assert with_bot.seeds[0].chats[0].bot_role == "administrator"
        assert without.seeds[0].chats[0].bot_role is None

    def test_a_seed_user_keyed_only_by_username_still_works(self) -> None:
        config = from_dict({"seeds": [{"name": "s", "users": [{"username": "ana"}]}]})
        user = config.seeds[0].users[0]
        assert (user.key, user.username, user.first_name) == ("ana", "ana", "ana")


class TestFeatureMatching:
    """`feature_for_tags` is what lets an existing suite group by feature
    without being rewritten — it is also the thing that quietly files
    scenarios under the wrong feature if it gets loose."""

    @pytest.fixture
    def config(self) -> SandboxConfig:
        return replace(
            DEFAULT_CONFIG,
            features=(
                FeatureSpec(id="core_rules", title="Rules", tags=("rules",)),
                FeatureSpec(id="core_captcha", title="Captcha", tags=("captcha", "join_chain")),
            ),
        )

    def test_a_tag_matching_a_declared_alias_files_the_scenario(
        self, config: SandboxConfig
    ) -> None:
        assert config.feature_for_tags(["join_chain", "en"]) == "core_captcha"

    def test_the_feature_id_itself_always_matches(self, config: SandboxConfig) -> None:
        assert config.feature_for_tags(["core_rules"]) == "core_rules"

    def test_matching_is_case_insensitive(self, config: SandboxConfig) -> None:
        assert config.feature_for_tags(["Captcha"]) == "core_captcha"

    def test_a_scenario_matching_nothing_stays_unfiled(self, config: SandboxConfig) -> None:
        """Guessing would be worse than not guessing: a scenario filed under
        the wrong feature makes that feature look tested when it isn't."""
        assert config.feature_for_tags(["something_else"]) is None


class TestLoading:
    def test_an_explicit_path_that_does_not_exist_is_an_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A launcher naming a config file has stated an intent. Running with
        different data than it asked for is the kind of failure that gets
        diagnosed as "the bot is broken"."""
        monkeypatch.setenv("CB_SANDBOX_CONFIG", str(tmp_path / "nope.json"))
        with pytest.raises(FileNotFoundError):
            discover_config_path()

    def test_a_malformed_file_degrades_to_defaults(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A config typo that turns the tool into a stack trace teaches people
        to stop using the tool, not to fix the typo."""
        bad = tmp_path / "sandbox.config.json"
        bad.write_text("{ not json")
        monkeypatch.setenv("CB_SANDBOX_CONFIG", str(bad))
        config = load_config()
        assert config.bot == DEFAULT_CONFIG.bot
        assert config.seed_names() == DEFAULT_CONFIG.seed_names()

    def test_environment_overrides_beat_the_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A process launcher has to be able to vary the database and the
        identity per run without writing a second config file — that is what
        makes one config file usable from a test session, a container and a
        terminal at once."""
        path = tmp_path / "sandbox.config.json"
        path.write_text(json.dumps({"bot": {"id": 1, "username": "from_file"}, "db": "file.db"}))
        monkeypatch.setenv("CB_SANDBOX_CONFIG", str(path))
        monkeypatch.setenv("CB_SANDBOX_BOT_USERNAME", "from_env")
        monkeypatch.setenv("CB_SANDBOX_DB", "/tmp/env.duckdb")
        config = load_config()
        assert config.bot.username == "from_env"
        assert config.bot.id == 1  # untouched by the env, still from the file
        assert config.db_path == "/tmp/env.duckdb"

    def test_discovery_walks_up_from_the_working_directory(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("CB_SANDBOX_CONFIG", raising=False)
        (tmp_path / "sandbox.config.json").write_text("{}")
        nested = tmp_path / "a" / "b"
        nested.mkdir(parents=True)
        assert discover_config_path(nested) == tmp_path / "sandbox.config.json"


def test_a_config_can_be_built_in_code_without_a_file() -> None:
    """A host application that already knows its bot should not have to write
    a JSON file to tell the sandbox about it."""
    config = SandboxConfig(
        bot=BotIdentity(id=5, username="inline", first_name="Inline"),
        seeds=(SeedFixture(name="only"),),
        default_seed="only",
    )
    assert config.seed("only") is not None
    assert config.bot.username == "inline"
