"""Unit coverage for `cb_worker.jobs.publisher` — the render, the fan-out skip
order, and the delivery sweep's failure taxonomy.

No Telegram session and no real network: the bot is an `AsyncMock` and the
exchange-rate call goes through `httpx.MockTransport` via `set_http_client`,
the pattern `test_youtube_job.py` established. Contract:
`docs/contracts/util_postforwarder.md`.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from cb_core import scheduled_posts
from cb_worker.jobs import publisher as job


@pytest.fixture(autouse=True)
def _reset_client() -> Iterator[None]:
    job.set_http_client(None)
    yield
    job.set_http_client(None)


def _transport(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _settings(**overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "exchangerate_api_key": "k",
        "exchangerate_timeout_seconds": 10.0,
        "postmail_chat_id": -100777,
        "postmail_chat_link": "https://t.me/Mural",
        "approval_chat_id": -100888,
        "publisher_hidden_author_names": ("Mekhy",),
        "owner_id": 5,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _post(**overrides: Any) -> scheduled_posts.ScheduledPost:
    base: dict[str, Any] = {
        "group_id": -1001,
        "post_id": uuid.uuid4(),
        "origin_title": "FurShop",
        "target_title": "Some Group",
        "days_remaining": 3,
        "next_run_at": datetime.now(UTC) - timedelta(minutes=1),
        "source_chat_id": -100777,
        "source_message_id": 42,
        "requester_chat_id": -1002,
        "requester_message_id": 7,
        "requester_user_id": 99,
    }
    base.update(overrides)
    return scheduled_posts.ScheduledPost(**base)


# ------------------------------------------------------------------- rate lookup


@pytest.mark.asyncio
async def test_rate_is_fetched_once_per_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    """v1 issued one request per priced *paragraph* (`Publisher.py:166-168`)."""
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"conversion_rates": {"BRL": 5.0}})

    job.set_http_client(_transport(handler))
    monkeypatch.setattr(job, "get_settings", _settings)
    rates = job._RateLookup()  # noqa: SLF001 - the memoisation is the unit
    await rates.warm("USD", "BRL")
    await rates.warm("USD", "BRL")
    assert calls == 1
    assert rates("USD", "BRL") == 5.0


@pytest.mark.asyncio
async def test_a_failed_rate_call_degrades_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """v1's per-paragraph `except Exception` leaves the paragraph unchanged."""
    job.set_http_client(_transport(lambda _: httpx.Response(500)))
    monkeypatch.setattr(job, "get_settings", _settings)
    rates = job._RateLookup()  # noqa: SLF001
    await rates.warm("USD", "BRL")
    assert rates("USD", "BRL") is None


@pytest.mark.asyncio
async def test_no_api_key_means_no_request(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(_: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError("no request should be made without a key")

    job.set_http_client(_transport(handler))
    monkeypatch.setattr(job, "get_settings", lambda: _settings(exchangerate_api_key=""))
    rates = job._RateLookup()  # noqa: SLF001
    await rates.warm("USD", "BRL")
    assert rates("USD", "BRL") is None


# -------------------------------------------------------------------- translation


@pytest.mark.asyncio
async def test_translation_failure_returns_the_original(monkeypatch: pytest.MonkeyPatch) -> None:
    """v1 reaches the same outcome by sniffing `'Error 500 (Server Error)'` in
    the reply and discarding it (`:206-209`); design R5.2 ports the effect."""

    def boom() -> Any:
        raise RuntimeError("no router")

    monkeypatch.setattr(job, "router", boom)
    assert await job._translate("hello", "English", group_id=1) == "hello"  # noqa: SLF001


@pytest.mark.asyncio
async def test_blank_text_is_never_sent_to_the_model(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> Any:  # pragma: no cover - must not run
        raise AssertionError("an empty caption must not cost a completion")

    monkeypatch.setattr(job, "router", boom)
    assert await job._translate("   ", "English", group_id=1) == "   "  # noqa: SLF001


# ------------------------------------------------------------------ retry safety


@pytest.mark.asyncio
async def test_a_failed_render_leaves_the_submission_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """arq retries this job, so consuming the pending post up front loses the
    campaign on any mid-job failure.

    v1's `prepare_post` pops the cache entry as its last act (`:219-220`),
    which is safe only because v1's version cannot be retried — it ran inline
    in the callback handler. Ported verbatim, a Telegram 5xx during the second
    upload would leave the retry with nothing to render: it would answer
    `publish_expired`, and the half-posted campaign would vanish with no trace
    beyond one orphaned Mural message.
    """
    from cb_core import pending_posts

    stored = pending_posts.PendingPost(media_kind="photo", file_id="f", caption="c")
    discarded: list[str] = []

    async def _get(_key: str) -> Any:
        return stored

    async def _discard(key: str) -> None:
        discarded.append(str(key))

    monkeypatch.setattr(pending_posts, "get", _get)
    monkeypatch.setattr(pending_posts, "discard", _discard)
    monkeypatch.setattr(job, "get_settings", _settings)

    bot = AsyncMock()
    bot.get_chat.return_value = SimpleNamespace(title="T", username="t", is_forum=False)
    bot.get_chat_member.return_value = SimpleNamespace(
        user=SimpleNamespace(first_name="Ana", username="ana")
    )
    # The second upload is the interesting one: the first already posted into
    # the Mural, so this is the state a naive retry would strand.
    bot.send_photo.side_effect = [SimpleNamespace(message_id=1), TimeoutError("upload died")]

    with pytest.raises(TimeoutError):
        await job._run_approve(  # noqa: SLF001
            bot,
            pending_key="77",
            origin_chat_id=-1001,
            requester_chat_id=-1002,
            requester_message_id=7,
            requester_user_id=99,
            days=7,
            has_nsfw=False,
        )

    assert discarded == [], "a failed render must leave the post for the retry"


@pytest.mark.asyncio
async def test_a_missing_submission_says_so_instead_of_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-PF-3: v1 raised KeyError into the global traceback handler and told
    nobody (`:183`)."""
    from cb_core import pending_posts

    async def _get(_key: str) -> Any:
        return None

    monkeypatch.setattr(pending_posts, "get", _get)
    monkeypatch.setattr(job, "get_settings", _settings)

    bot = AsyncMock()
    await job._run_approve(  # noqa: SLF001
        bot,
        pending_key="404",
        origin_chat_id=-1001,
        requester_chat_id=-1002,
        requester_message_id=7,
        requester_user_id=99,
        days=7,
        has_nsfw=False,
    )
    bot.send_photo.assert_not_awaited()
    bot.send_message.assert_awaited_once()


# ---------------------------------------------------------------------- scheduling


def test_next_run_is_always_tomorrow() -> None:
    """v1's `create_job` adds a day unconditionally (`:96`), so the first
    delivery never happens today even when the drawn time is still ahead."""
    now = datetime.now(UTC)
    assert job._next_run_at(23, 59) > now  # noqa: SLF001
    assert job._next_run_at(0, 0) > now  # noqa: SLF001


# ---------------------------------------------------------------------- the delivery


class _Sweep:
    """Records what the delivery sweep did to each row."""

    def __init__(self) -> None:
        self.deleted: list[uuid.UUID] = []
        self.advanced: list[uuid.UUID] = []

    def install(
        self, monkeypatch: pytest.MonkeyPatch, rows: list[Any], *, publisher_post: bool = True
    ) -> None:
        async def due_before(_moment: Any, *, limit: int = 500) -> tuple[Any, ...]:
            return tuple(rows)

        async def advance_or_expire(post: Any, _next: Any) -> bool:
            self.advanced.append(post.post_id)
            return True

        async def delete(_group_id: int, post_id: uuid.UUID) -> None:
            self.deleted.append(post_id)

        monkeypatch.setattr(scheduled_posts, "due_before", due_before)
        monkeypatch.setattr(scheduled_posts, "advance_or_expire", advance_or_expire)
        monkeypatch.setattr(scheduled_posts, "delete", delete)

        from cb_core import group_config

        async def get_config(_group_id: int) -> Any:
            return SimpleNamespace(publisher_post=publisher_post, thread_posts=None)

        monkeypatch.setattr(group_config, "get_config", get_config)


@pytest.mark.asyncio
async def test_a_due_row_is_forwarded_and_the_day_is_spent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post = _post()
    sweep = _Sweep()
    sweep.install(monkeypatch, [post])
    bot = AsyncMock()

    assert await job._run_delivery(bot) == 1  # noqa: SLF001
    bot.forward_message.assert_awaited_once_with(
        post.group_id, post.source_chat_id, post.source_message_id, message_thread_id=None
    )
    # D-PF-9, preserved: v1 decrements *before* the send (`:335-339`).
    assert sweep.advanced == [post.post_id]
    assert sweep.deleted == []


@pytest.mark.asyncio
async def test_opting_out_deletes_the_row_rather_than_pausing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-PG-4, preserved (`:342-345`): withdrawal of consent is final."""
    post = _post()
    sweep = _Sweep()
    sweep.install(monkeypatch, [post], publisher_post=False)
    bot = AsyncMock()

    assert await job._run_delivery(bot) == 0  # noqa: SLF001
    bot.forward_message.assert_not_awaited()
    assert sweep.deleted == [post.post_id]


@pytest.mark.asyncio
async def test_being_kicked_drops_the_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """v1's `BotWasKickedError` branch (`:352-354`)."""
    post = _post()
    sweep = _Sweep()
    sweep.install(monkeypatch, [post])
    bot = AsyncMock()
    bot.forward_message.side_effect = TelegramForbiddenError(method=None, message="kicked")  # type: ignore[arg-type]

    assert await job._run_delivery(bot) == 0  # noqa: SLF001
    assert sweep.deleted == [post.post_id]


@pytest.mark.asyncio
async def test_a_bad_request_drops_the_row(monkeypatch: pytest.MonkeyPatch) -> None:
    post = _post()
    sweep = _Sweep()
    sweep.install(monkeypatch, [post])
    bot = AsyncMock()
    bot.forward_message.side_effect = TelegramBadRequest(method=None, message="chat not found")  # type: ignore[arg-type]

    assert await job._run_delivery(bot) == 0  # noqa: SLF001
    assert sweep.deleted == [post.post_id]


@pytest.mark.asyncio
async def test_a_transient_failure_keeps_the_row_for_the_next_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-PF-8, fixed. v1 deleted the row for *any* non-kick exception
    (`:355-357`), so one 5xx ended the campaign for that group."""
    post = _post()
    sweep = _Sweep()
    sweep.install(monkeypatch, [post])
    bot = AsyncMock()
    bot.forward_message.side_effect = TimeoutError("upstream hiccup")

    assert await job._run_delivery(bot) == 0  # noqa: SLF001
    assert sweep.deleted == []
    # The day is still spent, so a permanently broken target drains on its
    # original schedule rather than living forever.
    assert sweep.advanced == [post.post_id]


# ----------------------------------------------------------------- the fan-out gates


def _target(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "group_id": -1001,
        "title": "Target Group",
        "publisher_post": True,
        "sfw": True,
        "language": "en",
        "publisher_members_only": False,
        "max_posts": 9999,
    }
    base.update(overrides)
    return base


async def _fan_out_with(
    monkeypatch: pytest.MonkeyPatch, targets: list[dict[str, Any]], **kwargs: Any
) -> list[dict[str, Any]]:
    """Run the fan-out over `targets`, returning the rows it created."""
    created: list[dict[str, Any]] = []

    from cb_core import db

    async def fetch(_stmt: str, *_args: Any, name: str = "") -> list[dict[str, Any]]:
        return targets

    async def create(**row: Any) -> uuid.UUID:
        created.append(row)
        return uuid.uuid4()

    monkeypatch.setattr(db, "fetch", fetch)
    monkeypatch.setattr(scheduled_posts, "create", create)
    monkeypatch.setattr(scheduled_posts, "delete_by_origin_title", AsyncMock(return_value=0))
    monkeypatch.setattr(scheduled_posts, "trim_to_max", AsyncMock(return_value=0))
    monkeypatch.setattr(job, "get_settings", _settings)

    call: dict[str, Any] = {
        "origin_title": "FurShop",
        "author_username": "ana",
        "days": 7,
        "has_nsfw": False,
        "requester_chat_id": -1002,
        "requester_message_id": 7,
        "requester_user_id": 99,
        "sent_pt": 11,
        "sent_en": 22,
    }
    call.update(kwargs)
    await job._fan_out(AsyncMock(), **call)  # noqa: SLF001 - the skip order is the unit
    return created


@pytest.mark.asyncio
async def test_a_group_that_opted_out_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """v1 `:249`: `if (not publisherpost) ... continue`."""
    created = await _fan_out_with(monkeypatch, [_target(publisher_post=False)])
    assert created == []


@pytest.mark.asyncio
async def test_an_nsfw_post_is_kept_out_of_an_sfw_group(monkeypatch: pytest.MonkeyPatch) -> None:
    """v1 `:249`: `has_nsfw == '1' and sfw`."""
    created = await _fan_out_with(monkeypatch, [_target(sfw=True)], has_nsfw=True)
    assert created == []
    created = await _fan_out_with(monkeypatch, [_target(sfw=False)], has_nsfw=True)
    assert len(created) == 1


@pytest.mark.asyncio
async def test_members_only_requires_the_author_in_the_roster(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v1 `:251-257`. D-PF-12: v1 ran `username not in str(members)` — a
    substring test over the stringified list, so an author called `bob` passed
    in any group containing a `bobby`. This compares set membership."""
    from cb_core import members

    monkeypatch.setattr(
        members, "roster", AsyncMock(return_value=(SimpleNamespace(username="bobby"),))
    )
    created = await _fan_out_with(
        monkeypatch, [_target(publisher_members_only=True)], author_username="bob"
    )
    assert created == []

    monkeypatch.setattr(
        members, "roster", AsyncMock(return_value=(SimpleNamespace(username="bob"),))
    )
    created = await _fan_out_with(
        monkeypatch, [_target(publisher_members_only=True)], author_username="bob"
    )
    assert len(created) == 1


@pytest.mark.asyncio
async def test_the_targets_language_picks_which_rendered_caption_is_forwarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v1 `:270`: `sent_pt if language == 'pt' else sent_en`."""
    created = await _fan_out_with(monkeypatch, [_target(language="pt")])
    assert created[0]["source_message_id"] == 11
    created = await _fan_out_with(monkeypatch, [_target(language="es")])
    assert created[0]["source_message_id"] == 22


@pytest.mark.asyncio
async def test_the_report_lists_every_scheduled_group_and_v1s_footer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v1 `:243,274,278`."""
    from cb_core import db

    async def fetch(_stmt: str, *_args: Any, name: str = "") -> list[dict[str, Any]]:
        return [_target(title="Alpha"), _target(group_id=-1002, title="Beta")]

    monkeypatch.setattr(db, "fetch", fetch)
    monkeypatch.setattr(scheduled_posts, "create", AsyncMock(return_value=uuid.uuid4()))
    monkeypatch.setattr(scheduled_posts, "delete_by_origin_title", AsyncMock(return_value=0))
    monkeypatch.setattr(scheduled_posts, "trim_to_max", AsyncMock(return_value=0))
    monkeypatch.setattr(job, "get_settings", _settings)

    report = await job._fan_out(  # noqa: SLF001
        AsyncMock(),
        origin_title="FurShop",
        author_username="ana",
        days=7,
        has_nsfw=False,
        requester_chat_id=-1002,
        requester_message_id=7,
        requester_user_id=99,
        sent_pt=11,
        sent_en=22,
    )
    assert report.startswith("Post set for the following times (7 days):\nNOW - Cookiebot Mural 📬")
    assert "- Alpha" in report
    assert "- Beta" in report
    assert report.endswith("OBS: private chats are not listed!")


# ------------------------------------------------------------------ the forum topic


@pytest.mark.asyncio
async def test_no_configured_topic_means_no_thread_argument() -> None:
    """D-PG-1, fixed. v1 passed its own `"9999"` sentinel straight into
    `message_thread_id` (`:348-349`), so a forum group that never set a topic
    got a failing forward — which its catch-all then punished by deleting the
    row. v2 normalises the sentinel to NULL at the storage layer."""
    bot = AsyncMock()
    assert await job._thread_id(bot, -1001, None) is None  # noqa: SLF001
    bot.get_chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_topic_is_only_used_when_the_chat_is_a_forum() -> None:
    bot = AsyncMock()
    bot.get_chat.return_value = SimpleNamespace(is_forum=False)
    assert await job._thread_id(bot, -1001, "12") is None  # noqa: SLF001

    bot.get_chat.return_value = SimpleNamespace(is_forum=True)
    assert await job._thread_id(bot, -1001, "12") == 12  # noqa: SLF001


@pytest.mark.asyncio
async def test_an_unparseable_topic_falls_back_to_the_general_thread() -> None:
    bot = AsyncMock()
    bot.get_chat.return_value = SimpleNamespace(is_forum=True)
    assert await job._thread_id(bot, -1001, "not-a-number") is None  # noqa: SLF001
