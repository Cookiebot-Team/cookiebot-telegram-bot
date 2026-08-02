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
