"""Unit tests for util_calladms's pure logic: callback-data wire shape, the
600-second staleness window, and the admin-username fetch's failure handling.

The confirmation flow itself (prompt -> press -> group ping) against the mock
Telegram API lives in `qa/test_util_calladms.py`; this file is everything in
between — no Telegram session, no database, just the transformations a v1
parity bug hides in. Model: `packages/cb-gateway/tests/test_config_menu.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from cb_core.textmatch import parse_command
from cb_gateway.handlers import calladms as cm

GROUP_ID = -1001234567890


# --------------------------------------------------------------- alias parity


class TestAliasesResolveToCalladms:
    """Phase 6 checklist: every v1 alias for this feature resolves.

    `/adm`, `/admin` and `/report` -> `calladms` are declared in
    `cb_core/textmatch.py:COMMAND_ALIASES` (out of this port's file ownership);
    this only asserts the observable behaviour this handler depends on.
    """

    @pytest.mark.parametrize("alias", ["adm", "admin", "report"])
    def test_alias_resolves(self, alias: str) -> None:
        parsed = parse_command(f"/{alias}")
        assert parsed is not None
        assert parsed.name == "calladms"

    def test_bare_word_triggers_are_not_recognised(self) -> None:
        """v1 also matches bare `@admin`/`@adm` with no leading slash
        (`COOKIEBOT.py:274`); `parse_command` only recognises a leading `/`, and
        fixing that is out of this port's file ownership (docs/contracts/util_calladms.md).
        Documented here as a known gap, not a silent regression.
        """
        assert parse_command("@admin") is None
        assert parse_command("@adm") is None


# --------------------------------------------------------------- callback wire


class TestCallbackData:
    @pytest.mark.parametrize("confirmed", [True, False])
    @pytest.mark.parametrize("message_id", [1, 999999, 42])
    def test_round_trips(self, confirmed: bool, message_id: int) -> None:
        data = cm.build_callback_data(confirmed, message_id)
        assert cm.parse_callback_data(data) == (confirmed, message_id)

    def test_yes_and_no_are_distinct(self) -> None:
        yes = cm.build_callback_data(True, 5)
        no = cm.build_callback_data(False, 5)
        assert yes != no
        assert cm.parse_callback_data(yes) == (True, 5)
        assert cm.parse_callback_data(no) == (False, 5)

    @pytest.mark.parametrize(
        "data",
        [
            "",
            "garbage",
            "CALLADMS",
            "CALLADMS YES",
            "CALLADMS MAYBE 5",
            "CALLADMS YES notanumber",
            "CALLADMS YES 5 extra",
            "WRONGTOKEN YES 5",
            "A CONFIG 5",  # a different feature's callback shape
        ],
    )
    def test_malformed_or_unrelated_is_none(self, data: str) -> None:
        assert cm.parse_callback_data(data) is None


# ----------------------------------------------------------------- staleness


class TestIsStale:
    def test_fresh_prompt_is_not_stale(self) -> None:
        now = datetime.now(UTC)
        prompt_date = now - timedelta(seconds=1)
        assert cm.is_stale(prompt_date, now=now) is False

    def test_exactly_at_the_boundary_is_not_stale(self) -> None:
        """v1's check is a strict `>` (`COOKIEBOT.py:401`): exactly 600s is fine."""
        now = datetime.now(UTC)
        prompt_date = now - timedelta(seconds=cm.STALE_AFTER_SECONDS)
        assert cm.is_stale(prompt_date, now=now) is False

    def test_one_second_past_the_boundary_is_stale(self) -> None:
        now = datetime.now(UTC)
        prompt_date = now - timedelta(seconds=cm.STALE_AFTER_SECONDS + 1)
        assert cm.is_stale(prompt_date, now=now) is True

    def test_long_past_is_stale(self) -> None:
        now = datetime.now(UTC)
        prompt_date = now - timedelta(hours=2)
        assert cm.is_stale(prompt_date, now=now) is True


# ------------------------------------------------------------ admin fetch


class _FakeUser:
    def __init__(self, username: str | None) -> None:
        self.username = username


class _FakeAdmin:
    def __init__(self, username: str | None) -> None:
        self.user = _FakeUser(username)


class _FakeBot:
    def __init__(self, admins: list[Any] | None = None, *, fail: bool = False) -> None:
        self._admins = admins or []
        self._fail = fail

    async def get_chat_administrators(self, chat_id: int) -> list[Any]:
        if self._fail:
            raise RuntimeError("Telegram is down")
        return self._admins


class TestAdminUsernames:
    async def test_returns_usernames_in_order(self) -> None:
        bot = _FakeBot([_FakeAdmin("alice"), _FakeAdmin("bob")])
        assert await cm.admin_usernames(bot, GROUP_ID) == ["alice", "bob"]  # type: ignore[arg-type]

    async def test_admins_without_a_username_are_skipped(self) -> None:
        """v1: `username = user['username'] if 'username' in user else None; if
        username: listaadmins.append(username)` (`Configurations.py:68-72`) —
        an admin with no username is silently excluded from the mention list,
        same as here.
        """
        bot = _FakeBot([_FakeAdmin("alice"), _FakeAdmin(None), _FakeAdmin("carol")])
        assert await cm.admin_usernames(bot, GROUP_ID) == ["alice", "carol"]  # type: ignore[arg-type]

    async def test_empty_admin_list_is_empty_mentions(self) -> None:
        bot = _FakeBot([])
        assert await cm.admin_usernames(bot, GROUP_ID) == []  # type: ignore[arg-type]

    async def test_telegram_failure_degrades_to_no_mentions(self) -> None:
        """v1's `get_admins` has no failure handling at all and would drop the
        whole update silently (`docs/contracts/admins.md`); this degrades
        instead of raising.
        """
        bot = _FakeBot(fail=True)
        assert await cm.admin_usernames(bot, GROUP_ID) == []  # type: ignore[arg-type]


class TestBareMentionTriggers:
    """v1 fires on four prefixes, not two.

    `COOKIEBOT.py:274` reads
    `msg['text'].startswith(("/adm", "@admin", "@adm", "/report"))`, so a group
    that has trained its members to type `@admin` keeps working. The slash forms
    come through COMMAND_ALIASES; these two cannot, because `parse_command` only
    inspects `/`-prefixed text.
    """

    @pytest.mark.parametrize("text", ["@admin", "@adm", "@Admin help", "@ADM please"])
    def test_v1_mention_forms_trigger(self, text: str) -> None:
        message = SimpleNamespace(text=text)
        assert cm._is_mention_trigger(message) is True  # noqa: SLF001

    @pytest.mark.parametrize(
        "text",
        ["@administrator_bob hello", "hey @admin", "@admins", "admin", "", "@adminfoo"],
    )
    def test_non_triggers(self, text: str) -> None:
        """A mention of someone whose name merely starts with 'adm' is not a call.

        v1's bare `startswith` did fire on `@admins` and `@adminfoo`; the word
        boundary here is a deliberate narrowing, recorded in the contract.
        """
        message = SimpleNamespace(text=text)
        assert cm._is_mention_trigger(message) is False  # noqa: SLF001
