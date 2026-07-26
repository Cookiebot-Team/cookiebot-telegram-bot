"""Unit tests for admin resolution — no Postgres, no Valkey.

Telegram is the outside world, so the bot is a hand-rolled fake. Our own cache
and db modules are monkeypatched at their public seams (`cb_core.cache.get_json`
/ `set_json`, `cb_core.db.fetch` / `db.transaction`) rather than replaced wholesale,
per the "don't fake our own code" rule for the layers above this one.
"""

from __future__ import annotations

from typing import Any

import pytest
from aiogram.types import Chat, ChatMemberAdministrator, ChatMemberOwner, Message, User
from prometheus_client import Counter

from cb_core import admins as admins_mod
from cb_core.admins import (
    ANONYMOUS_BOT_ID,
    Admin,
    is_anonymous_sender,
    resolve_actor,
)

# --------------------------------------------------------------------------- helpers


def make_message(*, from_user: User | None, chat: Chat, sender_chat: Chat | None = None) -> Message:
    return Message.model_construct(chat=chat, from_user=from_user, sender_chat=sender_chat)


def make_owner(user_id: int, *, is_anonymous: bool = False) -> ChatMemberOwner:
    return ChatMemberOwner(
        status="creator",
        user=User(id=user_id, is_bot=False, first_name="Owner"),
        is_anonymous=is_anonymous,
    )


def make_administrator(
    user_id: int,
    *,
    can_restrict_members: bool = False,
    can_delete_messages: bool = False,
    is_anonymous: bool = False,
) -> ChatMemberAdministrator:
    return ChatMemberAdministrator(
        status="administrator",
        user=User(id=user_id, is_bot=False, first_name=f"Admin{user_id}"),
        can_be_edited=False,
        is_anonymous=is_anonymous,
        can_manage_chat=True,
        can_delete_messages=can_delete_messages,
        can_manage_video_chats=True,
        can_restrict_members=can_restrict_members,
        can_promote_members=False,
        can_change_info=True,
        can_invite_users=True,
        can_post_stories=False,
        can_edit_stories=False,
        can_delete_stories=False,
    )


class FakeBot:
    """Stands in for aiogram's `Bot.get_chat_administrators`."""

    def __init__(self, members: list[Any] | None = None, *, error: Exception | None = None) -> None:
        self.members = members or []
        self.error = error
        self.calls: list[int] = []

    async def get_chat_administrators(self, chat_id: int) -> list[Any]:
        self.calls.append(chat_id)
        if self.error is not None:
            raise self.error
        return self.members


class FakeL2Cache:
    """A dict-backed stand-in for Valkey's get_json/set_json, TTL ignored/explicit."""

    def __init__(self) -> None:
        self.store: dict[str, Any] = {}

    async def get_json(self, key: str) -> Any | None:
        return self.store.get(key)

    async def set_json(self, key: str, value: Any, ttl_seconds: int) -> None:
        self.store[key] = value

    def expire(self, key: str) -> None:
        self.store.pop(key, None)


@pytest.fixture(autouse=True)
def reset_l1() -> None:
    admins_mod._l1.clear()  # noqa: SLF001 - the L1 process cache is the seam this test owns
    yield
    admins_mod._l1.clear()  # noqa: SLF001 - same seam, tidy up after the test too


@pytest.fixture
def fake_l2(monkeypatch: pytest.MonkeyPatch) -> FakeL2Cache:
    fake = FakeL2Cache()
    monkeypatch.setattr(admins_mod.cache, "get_json", fake.get_json)
    monkeypatch.setattr(admins_mod.cache, "set_json", fake.set_json)
    return fake


@pytest.fixture
def fake_clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """A controllable monotonic clock for L1 TTL tests."""
    box = [1_000.0]
    monkeypatch.setattr(admins_mod, "_now", lambda: box[0])
    return box


class _FakeConn:
    """Stands in for the asyncpg connection `db.transaction()` yields."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, stmt: str, *args: Any) -> None:
        self.executed.append((stmt, args))

    async def executemany(self, stmt: str, rows: list[tuple[Any, ...]]) -> None:
        self.executed.append((stmt, tuple(rows)))


class _FakeTransaction:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


@pytest.fixture(autouse=True)
def fake_db(monkeypatch: pytest.MonkeyPatch) -> _FakeConn:
    """No Postgres here — `refresh()`'s persistence step gets a fake connection.

    Individual tests that care about exactly what got written monkeypatch
    `admins._persist` directly instead; this fixture just keeps the happy-path
    tests (caching, resolve_actor) from needing a real database at all.
    """
    conn = _FakeConn()
    monkeypatch.setattr(admins_mod.db, "transaction", lambda: _FakeTransaction(conn))
    return conn


# --------------------------------------------------------------------------- is_anonymous_sender


class TestIsAnonymousSender:
    def test_normal_message(self) -> None:
        chat = Chat(id=-100123, type="supergroup")
        user = User(id=555, is_bot=False, first_name="Alice")
        message = make_message(from_user=user, chat=chat)
        assert is_anonymous_sender(message) is False

    def test_sender_chat_is_the_group(self) -> None:
        chat = Chat(id=-100123, type="supergroup")
        anon_user = User(id=ANONYMOUS_BOT_ID, is_bot=True, first_name="Group")
        message = make_message(from_user=anon_user, chat=chat, sender_chat=chat)
        assert is_anonymous_sender(message) is True

    def test_group_anonymous_bot_without_matching_sender_chat(self) -> None:
        # Belt-and-suspenders: from_user alone is enough even if sender_chat is
        # absent (defensive against a transport that only surfaces one signal).
        chat = Chat(id=-100123, type="supergroup")
        anon_user = User(id=ANONYMOUS_BOT_ID, is_bot=True, first_name="Group")
        message = make_message(from_user=anon_user, chat=chat, sender_chat=None)
        assert is_anonymous_sender(message) is True

    def test_linked_channel_post_is_not_anonymous_admin(self) -> None:
        # sender_chat present but it's a *different* chat (a linked discussion
        # channel), not the group itself — v1's bare `'sender_chat' not in msg`
        # check would have wrongly treated this as an anonymous admin.
        chat = Chat(id=-100123, type="supergroup")
        channel = Chat(id=-100999, type="channel")
        user = User(id=777, is_bot=True, first_name="Telegram")
        message = make_message(from_user=user, chat=chat, sender_chat=channel)
        assert is_anonymous_sender(message) is False


# --------------------------------------------------------------------------- caching


class TestCaching:
    async def test_miss_then_l1_hit(self, fake_l2: FakeL2Cache, fake_clock: list[float]) -> None:
        bot = FakeBot([make_owner(1)])
        result1 = await admins_mod.admins(bot, group_id=-1)
        assert [a.user_id for a in result1] == [1]
        assert len(bot.calls) == 1

        result2 = await admins_mod.admins(bot, group_id=-1)
        assert result2 == result1
        assert len(bot.calls) == 1, "L1 hit must not re-fetch Telegram"

    async def test_l1_expiry_falls_back_to_l2(
        self, fake_l2: FakeL2Cache, fake_clock: list[float]
    ) -> None:
        bot = FakeBot([make_owner(1)])
        await admins_mod.admins(bot, group_id=-2)
        assert len(bot.calls) == 1

        # Move past the L1 TTL but leave the L2 (Valkey) entry alone.
        fake_clock[0] += admins_mod.get_settings().config_cache_l1_seconds + 1

        result = await admins_mod.admins(bot, group_id=-2)
        assert [a.user_id for a in result] == [1]
        assert len(bot.calls) == 1, "L2 hit must not re-fetch Telegram"

    async def test_l2_expiry_refetches_from_telegram(
        self, fake_l2: FakeL2Cache, fake_clock: list[float]
    ) -> None:
        bot = FakeBot([make_owner(1)])
        await admins_mod.admins(bot, group_id=-3)
        assert len(bot.calls) == 1

        fake_clock[0] += admins_mod.get_settings().config_cache_l1_seconds + 1
        fake_l2.expire(f"{admins_mod._CACHE_PREFIX}-3")  # noqa: SLF001 - test owns this cache-key seam

        result = await admins_mod.admins(bot, group_id=-3)
        assert [a.user_id for a in result] == [1]
        assert len(bot.calls) == 2, "L1 and L2 miss must re-fetch Telegram"

    async def test_cache_metrics_labelled(
        self, fake_l2: FakeL2Cache, fake_clock: list[float], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[tuple[str, str, str]] = []
        real_labels = admins_mod.metrics.cache_lookups_total.labels

        def spy_labels(*, cache: str, layer: str, outcome: str) -> Counter:
            seen.append((cache, layer, outcome))
            return real_labels(cache=cache, layer=layer, outcome=outcome)

        monkeypatch.setattr(admins_mod.metrics.cache_lookups_total, "labels", spy_labels)
        bot = FakeBot([make_owner(1)])
        await admins_mod.admins(bot, group_id=-4)
        assert ("admins", "l1", "miss") in seen
        assert ("admins", "l2", "miss") in seen
        assert ("admins", "telegram", "hit") in seen


# --------------------------------------------------------------------------- telegram failure fallback


class TestTelegramFailureFallback:
    async def test_falls_back_to_persisted_rows(
        self, fake_l2: FakeL2Cache, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_fetch(*_args: Any, **_kwargs: Any) -> list[Any]:
            return [
                {"user_id": 42, "role": "administrator"},
                {"user_id": 1, "role": "creator"},
            ]

        monkeypatch.setattr(admins_mod.db, "fetch", fake_fetch)
        bot = FakeBot(error=RuntimeError("telegram is down"))

        result = await admins_mod.refresh(bot, group_id=-5)
        by_id = {a.user_id: a for a in result}
        assert set(by_id) == {42, 1}
        assert by_id[1].role == "creator"
        assert by_id[1].can_restrict_members is True
        assert by_id[42].role == "administrator"
        assert by_id[42].can_restrict_members is False, "lossy fallback defaults to no privilege"

    async def test_no_persisted_rows_means_nobody_is_admin(
        self, fake_l2: FakeL2Cache, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def empty_fetch(*_args: Any, **_kwargs: Any) -> list[Any]:
            return []

        monkeypatch.setattr(admins_mod.db, "fetch", empty_fetch)
        bot = FakeBot(error=RuntimeError("telegram is down"))

        result = await admins_mod.refresh(bot, group_id=-6)
        assert result == ()
        assert await admins_mod.is_admin(bot, group_id=-6, user_id=999) is False

    async def test_telegram_outage_never_grants_everyone_admin(
        self, fake_l2: FakeL2Cache, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def empty_fetch(*_args: Any, **_kwargs: Any) -> list[Any]:
            return []

        monkeypatch.setattr(admins_mod.db, "fetch", empty_fetch)
        bot = FakeBot(error=RuntimeError("telegram is down"))
        for candidate_user_id in (1, 2, 3, ANONYMOUS_BOT_ID):
            assert await admins_mod.is_admin(bot, group_id=-7, user_id=candidate_user_id) is False


# --------------------------------------------------------------------------- role / privilege parsing


class TestParsing:
    async def test_creator_gets_full_privileges_even_without_flags(
        self, fake_l2: FakeL2Cache, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}

        async def fake_persist(group_id: int, rows: tuple) -> None:
            captured["group_id"] = group_id
            captured["rows"] = rows

        monkeypatch.setattr(admins_mod, "_persist", fake_persist)
        bot = FakeBot([make_owner(10, is_anonymous=True)])

        result = await admins_mod.refresh(bot, group_id=-8)
        assert result == (
            Admin(user_id=10, role="creator", can_restrict_members=True, can_delete_messages=True),
        )
        # Telegram's per-admin anonymity flag reaches the DB write even though
        # it is not part of the public Admin dataclass.
        assert captured["rows"] == ((10, "creator", True),)

    async def test_administrator_privileges_parsed_from_payload(
        self, fake_l2: FakeL2Cache, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(admins_mod, "_persist", _noop_persist)
        bot = FakeBot(
            [make_administrator(20, can_restrict_members=True, can_delete_messages=False)]
        )
        result = await admins_mod.refresh(bot, group_id=-9)
        assert result == (
            Admin(
                user_id=20,
                role="administrator",
                can_restrict_members=True,
                can_delete_messages=False,
            ),
        )

    async def test_mixed_creator_and_administrators(
        self, fake_l2: FakeL2Cache, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(admins_mod, "_persist", _noop_persist)
        bot = FakeBot(
            [
                make_owner(1),
                make_administrator(2, can_restrict_members=True, can_delete_messages=True),
                make_administrator(3),
            ]
        )
        result = await admins_mod.refresh(bot, group_id=-10)
        roles = {a.user_id: a.role for a in result}
        assert roles == {1: "creator", 2: "administrator", 3: "administrator"}


async def _noop_persist(_group_id: int, _rows: tuple) -> None:
    return None


# --------------------------------------------------------------------------- resolve_actor


class TestResolveActor:
    async def test_anonymous_sender_is_trusted_as_admin(
        self, fake_l2: FakeL2Cache, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        chat = Chat(id=-100123, type="supergroup")
        anon_user = User(id=ANONYMOUS_BOT_ID, is_bot=True, first_name="Group")
        message = make_message(from_user=anon_user, chat=chat, sender_chat=chat)

        bot = FakeBot([])  # must never be consulted for an anonymous sender
        result = await resolve_actor(bot, message)
        assert result.user_id is None
        assert result.is_admin is True
        assert result.anonymous is True
        assert bot.calls == []

    async def test_real_admin_is_recognised(
        self, fake_l2: FakeL2Cache, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        chat = Chat(id=-100123, type="supergroup")
        user = User(id=1, is_bot=False, first_name="Alice")
        message = make_message(from_user=user, chat=chat)
        bot = FakeBot([make_owner(1)])

        result = await resolve_actor(bot, message)
        assert result == admins_mod.ActorCheck(user_id=1, is_admin=True, anonymous=False)

    async def test_non_admin_is_rejected(
        self, fake_l2: FakeL2Cache, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        chat = Chat(id=-100123, type="supergroup")
        user = User(id=2, is_bot=False, first_name="Bob")
        message = make_message(from_user=user, chat=chat)
        bot = FakeBot([make_owner(1)])

        result = await resolve_actor(bot, message)
        assert result == admins_mod.ActorCheck(user_id=2, is_admin=False, anonymous=False)

    async def test_no_from_user_and_not_anonymous_is_never_admin(
        self, fake_l2: FakeL2Cache
    ) -> None:
        chat = Chat(id=-100123, type="supergroup")
        message = make_message(from_user=None, chat=chat)
        bot = FakeBot([])

        result = await resolve_actor(bot, message)
        assert result == admins_mod.ActorCheck(user_id=None, is_admin=False, anonymous=False)


class TestInfrastructureOutages:
    """The module promises callers it never raises. These are the ways it could.

    A handler asking "is this user an admin?" must still get an answer during a
    Valkey or Postgres outage, because Telegram — the only source that actually
    knows — is a separate system that is probably still up. v1 had the opposite
    failure mode: any exception inside the update thread was swallowed whole
    (`COOKIEBOT.py:329-330`) and the command silently did nothing.
    """

    async def test_admin_check_survives_a_dead_cache_and_database(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def boom(*args: object, **kwargs: object) -> None:
            raise RuntimeError("cache not initialised; call init_cache() during startup")

        monkeypatch.setattr(admins_mod.cache, "get_json", boom)
        monkeypatch.setattr(admins_mod.cache, "set_json", boom)
        monkeypatch.setattr(admins_mod.db, "fetch", boom)
        monkeypatch.setattr(admins_mod.db, "transaction", boom)

        bot = FakeBot([make_administrator(700)])

        assert await admins_mod.is_admin(bot, -100777, 700) is True
        assert await admins_mod.is_admin(bot, -100777, 701) is False

    async def test_telegram_failure_with_no_database_makes_nobody_an_admin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def boom(*args: object, **kwargs: object) -> None:
            raise RuntimeError("no database")

        monkeypatch.setattr(admins_mod.cache, "get_json", boom)
        monkeypatch.setattr(admins_mod.cache, "set_json", boom)
        monkeypatch.setattr(admins_mod.db, "fetch", boom)

        bot = FakeBot(error=RuntimeError("telegram is down"))

        # Not "everyone", not an exception: nobody.
        assert await admins_mod.admins(bot, -100778) == ()
        assert await admins_mod.is_admin(bot, -100778, 700) is False
