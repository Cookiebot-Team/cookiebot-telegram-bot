"""Unit coverage for `cb_worker.jobs.birthday` — target resolution, photo
fallback, the deferred-follow-up scheduling, and the full collage flow
against a fake `Bot`. No real Telegram, no real database, no real photos
(the placeholder asset is real — it is package data, not a mock).
"""

from __future__ import annotations

import io
from typing import Any
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from cb_core import jobs
from cb_core.birthdays import BirthdayPerson
from cb_core.members import MemberRef
from cb_worker.jobs import birthday as job

_ALICE = MemberRef(user_id=1, username="alice")
_BOB = MemberRef(user_id=2, username="bob")


def _bot(**overrides: Any) -> AsyncMock:
    bot = AsyncMock()
    for name, value in overrides.items():
        setattr(bot, name, value)
    return bot


def _png_bytes(size: tuple[int, int] = (32, 32)) -> io.BytesIO:
    buf = io.BytesIO()
    Image.new("RGB", size, (255, 0, 0)).save(buf, format="PNG")
    buf.seek(0)
    return buf


class TestFindInRoster:
    def test_matches_case_insensitively(self) -> None:
        assert job._find_in_roster((_ALICE, _BOB), "ALICE") == 1  # noqa: SLF001

    def test_strips_the_at_sign(self) -> None:
        assert job._find_in_roster((_ALICE, _BOB), "@bob") == 2  # noqa: SLF001

    def test_no_match_is_none(self) -> None:
        assert job._find_in_roster((_ALICE, _BOB), "carol") is None  # noqa: SLF001


class TestTargets:
    def test_real_hits_and_extras_are_both_included(self) -> None:
        people = (BirthdayPerson(user_id=1, username="alice", first_name=None, last_name=None),)
        targets = job._targets(people, ["@bob"], (_ALICE, _BOB))  # noqa: SLF001
        assert targets == [("@alice", 1), ("@bob", 2)]

    def test_an_unresolvable_extra_still_appears_with_no_user_id(self) -> None:
        targets = job._targets((), ["stranger"], (_ALICE,))  # noqa: SLF001
        assert targets == [("@stranger", None)]

    def test_no_deduplication_between_real_hits_and_extras(self) -> None:
        """v1's own behaviour (Birthdays.py:41-42): a person tagged manually
        who also has a real birthdate on file appears twice."""
        people = (BirthdayPerson(user_id=1, username="alice", first_name=None, last_name=None),)
        targets = job._targets(people, ["@alice"], (_ALICE,))  # noqa: SLF001
        assert targets == [("@alice", 1), ("@alice", 1)]

    def test_empty_extra_tokens_are_skipped(self) -> None:
        targets = job._targets((), ["", "  ", "@"], (_ALICE,))  # noqa: SLF001
        assert targets == []


class TestPhotoFor:
    async def test_no_user_id_is_the_placeholder(self) -> None:
        image = await job._photo_for(_bot(), None)  # noqa: SLF001
        assert image.mode == "RGBA"

    async def test_a_resolved_photo_is_used(self) -> None:
        bot = _bot(
            get_user_profile_photos=AsyncMock(
                return_value=AsyncMock(photos=[[AsyncMock(file_id="f1")]])
            ),
            download=AsyncMock(return_value=_png_bytes()),
        )
        before = job.birthday_photo_total.labels(outcome="fetched")._value.get()  # noqa: SLF001
        image = await job._photo_for(bot, 1)  # noqa: SLF001
        assert image.mode == "RGBA"
        assert job.birthday_photo_total.labels(outcome="fetched")._value.get() == before + 1  # noqa: SLF001

    async def test_no_photos_available_falls_back_to_the_placeholder(self) -> None:
        bot = _bot(get_user_profile_photos=AsyncMock(return_value=AsyncMock(photos=[])))
        before = job.birthday_photo_total.labels(outcome="placeholder")._value.get()  # noqa: SLF001
        await job._photo_for(bot, 1)  # noqa: SLF001
        assert job.birthday_photo_total.labels(outcome="placeholder")._value.get() == before + 1  # noqa: SLF001

    async def test_a_raising_bot_call_falls_back_to_the_placeholder(self) -> None:
        bot = _bot(get_user_profile_photos=AsyncMock(side_effect=Exception("Telegram is down")))
        image = await job._photo_for(bot, 1)  # noqa: SLF001
        assert image.mode == "RGBA"


class TestScheduleFollowup:
    async def test_enqueues_with_the_right_defer(self) -> None:
        redis = AsyncMock()
        ctx: dict[str, Any] = {"redis": redis}
        await job._schedule_followup(ctx, group_id=555, lang="en")  # noqa: SLF001
        redis.enqueue_job.assert_awaited_once_with(
            jobs.NEXT_BIRTHDAYS_FOLLOWUP, group_id=555, lang="en", _defer_by=900
        )

    async def test_a_scheduling_failure_does_not_raise(self) -> None:
        redis = AsyncMock()
        redis.enqueue_job.side_effect = Exception("broker down")
        ctx: dict[str, Any] = {"redis": redis}
        await job._schedule_followup(ctx, group_id=555, lang="en")  # noqa: SLF001


class TestPost:
    async def test_no_targets_sends_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(job.birthdays, "members_with_birthday", AsyncMock(return_value=()))
        monkeypatch.setattr(job.members, "roster", AsyncMock(return_value=()))
        bot = _bot(send_photo=AsyncMock(), send_message=AsyncMock())
        ctx: dict[str, Any] = {"bot": bot, "redis": AsyncMock()}
        await job._post(ctx, 555, 42, [], "en")  # noqa: SLF001
        bot.send_photo.assert_not_awaited()

    async def test_a_real_hit_composites_pins_and_schedules_the_followup(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        people = (BirthdayPerson(user_id=1, username="alice", first_name=None, last_name=None),)
        monkeypatch.setattr(job.birthdays, "members_with_birthday", AsyncMock(return_value=people))
        monkeypatch.setattr(job.members, "roster", AsyncMock(return_value=(_ALICE,)))
        sent_message = AsyncMock()
        sent_message.message_id = 999
        bot = _bot(
            get_user_profile_photos=AsyncMock(return_value=AsyncMock(photos=[])),
            send_photo=AsyncMock(return_value=sent_message),
            pin_chat_message=AsyncMock(),
            send_message=AsyncMock(),
        )
        redis = AsyncMock()
        ctx: dict[str, Any] = {"bot": bot, "redis": redis}

        await job._post(ctx, 555, 42, [], "en")  # noqa: SLF001

        bot.send_photo.assert_awaited_once()
        assert bot.send_photo.await_args.kwargs["reply_to_message_id"] == 42
        assert "@alice" in bot.send_photo.await_args.kwargs["caption"]
        bot.pin_chat_message.assert_awaited_once_with(555, 999)
        bot.send_message.assert_awaited_once_with(555, "🎂")
        redis.enqueue_job.assert_awaited_once_with(
            jobs.NEXT_BIRTHDAYS_FOLLOWUP, group_id=555, lang="en", _defer_by=900
        )

    async def test_a_pin_failure_does_not_abort_the_rest(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        people = (BirthdayPerson(user_id=1, username="alice", first_name=None, last_name=None),)
        monkeypatch.setattr(job.birthdays, "members_with_birthday", AsyncMock(return_value=people))
        monkeypatch.setattr(job.members, "roster", AsyncMock(return_value=(_ALICE,)))
        sent_message = AsyncMock()
        sent_message.message_id = 999
        bot = _bot(
            get_user_profile_photos=AsyncMock(return_value=AsyncMock(photos=[])),
            send_photo=AsyncMock(return_value=sent_message),
            pin_chat_message=AsyncMock(side_effect=Exception("no pin rights")),
            send_message=AsyncMock(),
        )
        ctx: dict[str, Any] = {"bot": bot, "redis": AsyncMock()}
        await job._post(ctx, 555, 42, [], "en")  # noqa: SLF001
        bot.send_message.assert_awaited_once_with(555, "🎂")


class TestPostBirthdayCollageWrapper:
    async def test_runs_without_raising(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(job, "_post", AsyncMock())
        ctx: dict[str, Any] = {"bot": AsyncMock(), "redis": AsyncMock()}
        await job.post_birthday_collage(ctx, group_id=555, message_id=42, extra_names=[], lang="en")


class TestNextBirthdaysFollowup:
    async def test_sends_the_shared_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(job.birthdays, "next_birthdays_text", AsyncMock(return_value="text"))
        bot = _bot(send_message=AsyncMock())
        ctx: dict[str, Any] = {"bot": bot}
        await job.next_birthdays_followup(ctx, group_id=555, lang="en")
        bot.send_message.assert_awaited_once_with(555, "text")


# ------------------------------------------------- the daily broadcast (v1's
# `manual_chat_id=None` shape). See the job module's docstring for the caller
# this project could not previously find — `COOKIEBOT.py:333-339`.


class _SweepBot:
    """Only `get_chat`, which is all the sweep asks a bot for."""

    def __init__(self, pinned: dict[int, str | None] | None = None) -> None:
        self.pinned = pinned or {}
        self.asked: list[int] = []

    async def get_chat(self, group_id: int) -> object:
        self.asked.append(group_id)
        caption = self.pinned.get(group_id)

        class _Pinned:
            def __init__(self, text: str | None) -> None:
                self.caption = text

        class _Chat:
            def __init__(self, text: str | None) -> None:
                self.pinned_message = _Pinned(text) if text is not None else None

        return _Chat(caption)


class _Redis:
    def __init__(self) -> None:
        self.jobs: list[dict[str, object]] = []

    async def enqueue_job(self, name: str, **kwargs: object) -> None:
        self.jobs.append({"name": name, **kwargs})


@pytest.mark.parametrize(
    ("caption", "expected"),
    [
        (None, False),
        ("", False),
        ("just a pinned photo", False),
        # v1 checks three localised markers, case-insensitively (`:32`).
        ("<i>Happy birthday!</i>\n2026-08-06", True),
        ("<i>FELIZ ANIVERSÁRIO!</i>\n2026-08-06", True),
        ("<i>feliz cumpleaños!</i>\n2026-08-06", True),
        # Yesterday's post does not suppress today's (`:33`, `:44`).
        ("<i>Happy birthday!</i>\n2026-08-05", False),
        # The marker alone, with no date, is not today's either.
        ("<i>Happy birthday!</i>", False),
    ],
)
def test_pinned_dedup_matches_v1s_markers(caption: str | None, expected: bool) -> None:
    assert job.already_posted_today(caption, "2026-08-06") is expected


async def _sweep(monkeypatch: pytest.MonkeyPatch, **kw: object) -> tuple[_Redis, _SweepBot, int]:
    from cb_core.group_config import GroupConfig
    from cb_core.settings import Settings

    groups: tuple[int, ...] = kw.get("groups", (-100, -200))  # type: ignore[assignment]
    fun: dict[int, bool] = kw.get("fun", {})  # type: ignore[assignment]
    pinned: dict[int, str | None] = kw.get("pinned", {})  # type: ignore[assignment]
    enabled: bool = kw.get("enabled", True)  # type: ignore[assignment]

    async def _groups(month: int, day: int) -> tuple[int, ...]:
        return groups

    async def _config(group_id: int) -> GroupConfig:
        return GroupConfig(group_id=group_id, functions_fun=fun.get(group_id, True))

    monkeypatch.setattr(job.birthdays, "groups_with_birthdays", _groups)
    monkeypatch.setattr(job.group_config, "get_config", _config)
    monkeypatch.setattr(job, "get_settings", lambda: Settings(birthday_broadcast_enabled=enabled))

    redis, bot = _Redis(), _SweepBot(pinned)
    queued = await job.broadcast_birthdays({"bot": bot, "redis": redis})
    return redis, bot, queued


async def test_the_sweep_queues_one_job_per_eligible_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis, _bot, queued = await _sweep(monkeypatch)
    assert queued == 2
    assert [job["group_id"] for job in redis.jobs] == [-100, -200]


async def test_the_sweep_spaces_the_posts_instead_of_sleeping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v1 slept 3 seconds per group on a worker thread (FEATURE-MAP D8)."""
    redis, _bot, _ = await _sweep(monkeypatch)
    assert [job["_defer_by"] for job in redis.jobs] == [
        0,
        job.BROADCAST_SPACING_SECONDS,
    ]


async def test_a_group_with_fun_off_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """v1: `if not funfunctions: continue` (`Birthdays.py:24-26`)."""
    redis, _bot, queued = await _sweep(monkeypatch, fun={-100: False})
    assert queued == 1
    assert [job["group_id"] for job in redis.jobs] == [-200]


async def test_a_group_that_already_has_todays_post_pinned_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime

    today = datetime.now(UTC).date().isoformat()
    redis, _bot, queued = await _sweep(
        monkeypatch, pinned={-100: f"<i>Happy birthday!</i>\n{today}"}
    )
    assert queued == 1
    assert [job["group_id"] for job in redis.jobs] == [-200]


async def test_the_switch_stops_the_sweep_before_it_reads_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis, bot, queued = await _sweep(monkeypatch, enabled=False)
    assert (queued, redis.jobs, bot.asked) == (0, [], [])


async def test_a_day_with_no_birthdays_asks_telegram_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cheap path, and the common one: no group query result means no
    `getChat` per group, which is the round trip v1 made unconditionally."""
    redis, bot, queued = await _sweep(monkeypatch, groups=())
    assert (queued, redis.jobs, bot.asked) == (0, [], [])
