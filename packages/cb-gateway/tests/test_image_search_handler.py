"""Unit tests for x_image_search's reply-path half: which messages are search
candidates at all, and the daily quotas that used to be a per-process dict.

Named `..._handler` rather than `test_image_search.py` because pytest imports
test modules by basename when they are not inside a package, and
`packages/cb-core/tests/test_image_search.py` already owns that name.

The Google call and the send loop are `packages/cb-worker/tests/test_image_search_job.py`;
the dispatch behaviour (what the catch-all does and does not swallow) is
`qa/test_x_image_search.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from cb_gateway.handlers import image_search as handler


class TestIsSearchCandidate:
    def test_a_plain_unknown_command(self) -> None:
        assert handler.is_search_candidate("/french fries", "CookieMWbot") is True

    def test_a_bare_slash_is_not(self) -> None:
        """v1's enclosing `if` requires `len(text) > 1` (`COOKIEBOT.py:186`)."""
        assert handler.is_search_candidate("/", "CookieMWbot") is False

    def test_plain_text_is_not(self) -> None:
        assert handler.is_search_candidate("french fries", "CookieMWbot") is False

    def test_a_double_slash_anywhere_is_not(self) -> None:
        """v1: `"//" not in msg['text']` (`:283`) — a pasted URL is not a
        search."""
        assert handler.is_search_candidate("/see https://example.com", "CookieMWbot") is False
        assert handler.is_search_candidate("/a//b", "CookieMWbot") is False

    def test_addressed_at_this_bot_is_a_candidate(self) -> None:
        assert handler.is_search_candidate("/cat@CookieMWbot", "CookieMWbot") is True

    def test_addressed_at_this_bot_case_insensitively(self) -> None:
        assert handler.is_search_candidate("/cat@cookiemwbot", "CookieMWbot") is True

    def test_addressed_at_another_bot_is_not(self) -> None:
        """Telegram delivers `/cat@OtherBot` to every bot in the group; v1
        compared against its own five persona usernames (`:283`), v2 against
        the bot the update arrived on."""
        assert handler.is_search_candidate("/cat@OtherBot", "CookieMWbot") is False

    def test_an_unknown_own_username_refuses_an_addressed_command(self) -> None:
        """`bot_username` is empty only when `getMe` failed at startup
        (`bots.py:55-64`). Refusing is the safe direction: the alternative is
        answering commands aimed at another bot."""
        assert handler.is_search_candidate("/cat@OtherBot", "") is False


class TestQuotaKeys:
    def test_the_date_is_in_the_key(self) -> None:
        user_key, total_key = handler.quota_keys(7, now=datetime(2026, 8, 14, tzinfo=UTC))
        assert user_key == "cb:imgsearch:u:7:20260814"
        assert total_key == "cb:imgsearch:all:20260814"

    def test_two_users_do_not_share_a_key(self) -> None:
        first, _ = handler.quota_keys(7)
        second, _ = handler.quota_keys(8)
        assert first != second


class _FakeCache:
    """Counts calls per key, like `cache.incr_window` does in Valkey."""

    def __init__(self, *, fail: bool = False) -> None:
        self.counts: dict[str, int] = {}
        self.fail = fail

    async def incr_window(self, key: str, window_seconds: int) -> int:
        if self.fail:
            raise RuntimeError("valkey is down")
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]


@pytest.fixture
def limits(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(
        handler,
        "get_settings",
        lambda: SimpleNamespace(image_search_daily_per_user=3, image_search_daily_total=5),
    )


class TestWithinQuota:
    async def test_allows_up_to_the_per_user_cap_then_refuses(
        self, limits: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(handler, "cache", _FakeCache())
        assert [await handler.within_quota(1) for _ in range(4)] == [True, True, True, False]

    async def test_the_global_cap_refuses_a_user_who_is_still_under_theirs(
        self, limits: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _FakeCache()
        monkeypatch.setattr(handler, "cache", fake)
        # Five searches across two users exhausts the global cap of 5.
        for user_id in (1, 2):
            for _ in range(2):
                assert await handler.within_quota(user_id) is True
        assert await handler.within_quota(3) is True  # the fifth
        assert await handler.within_quota(3) is False  # the sixth: global cap

    async def test_a_refused_call_still_spends_the_global_budget(
        self, limits: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """v1 decrements both counters before checking either
        (`COOKIEBOT.py:284-285`), so a user over their own limit still costs
        the bot one of its 180. Preserved, warts and all."""
        fake = _FakeCache()
        monkeypatch.setattr(handler, "cache", fake)
        for _ in range(4):
            await handler.within_quota(1)
        _, total_key = handler.quota_keys(1)
        assert fake.counts[total_key] == 4

    async def test_a_cache_outage_fails_open(
        self, limits: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(handler, "cache", _FakeCache(fail=True))
        assert await handler.within_quota(1) is True
