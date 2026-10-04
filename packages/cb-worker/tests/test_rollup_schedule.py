"""Unit tests for the minute rollup (x_live_stats R1.1-R1.3): cron table and UTC day."""

from __future__ import annotations

import time
from typing import Any

import pytest
from whenever import Instant

from cb_worker import main


def test_rollup_today_cron_runs_every_minute_without_overlap() -> None:
    jobs = {c.name: c for c in main.WorkerSettings.cron_jobs}
    job = jobs["cron:rollup_today"]
    assert job.coroutine is main.rollup_today
    assert job.second == 0
    assert job.minute is None
    assert job.hour is None
    assert job.job_id == "rollup_today"  # refuses enqueue while a run is in flight
    assert job.timeout_s is not None
    assert job.timeout_s < 60
    assert main.rollup_today in main.WorkerSettings.functions


def test_yesterday_closing_crons_kept() -> None:
    jobs = {c.name: c for c in main.WorkerSettings.cron_jobs}
    assert (jobs["cron:rollup_yesterday"].hour, jobs["cron:rollup_yesterday"].minute) == (0, 20)
    assert (jobs["cron:rollup_llm_costs"].hour, jobs["cron:rollup_llm_costs"].minute) == (0, 25)


class _FrozenInstant:
    @staticmethod
    def now() -> Instant:
        # 23:30 UTC on Oct 4 is already Oct 5 in UTC+14.
        return Instant.from_utc(2026, 10, 4, 23, 30)


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    seen: list[tuple[str, Any]] = []

    async def fake_execute(sql: str, *args: Any, **_kw: Any) -> None:
        seen.append((sql, args[0]))

    async def fake_job(_name: str, _ctx: dict, fn: Any, *args: Any) -> Any:
        return await fn(*args)

    monkeypatch.setattr(main.db, "execute", fake_execute)
    monkeypatch.setattr(main, "_job", fake_job)
    monkeypatch.setattr(main, "Instant", _FrozenInstant)
    monkeypatch.setenv("TZ", "Pacific/Kiritimati")
    time.tzset()
    yield seen
    monkeypatch.undo()
    time.tzset()


async def test_rollup_today_uses_utc_day(calls: list[tuple[str, Any]]) -> None:
    await main.rollup_today({})
    days = {str(d) for _, d in calls}
    assert days == {"2026-10-04"}
    assert [s for s, _ in calls] == ["SELECT cb_rollup_day($1)", "SELECT cb_rollup_llm_day($1)"]


async def test_nightly_rollups_use_utc_days(calls: list[tuple[str, Any]]) -> None:
    await main.rollup_yesterday({})
    await main.rollup_llm_costs({})
    assert [str(d) for _, d in calls] == ["2026-10-03", "2026-10-04"] * 2
