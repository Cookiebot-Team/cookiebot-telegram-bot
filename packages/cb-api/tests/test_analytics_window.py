"""x_analytics_api's window resolution and summary arithmetic — the two pieces
that decide what a caller gets and neither of which needs a database.

The queries themselves are `qa/integration/test_analytics.py`, against real
rollup rows in real Citus; the HTTP surface (auth, 401/404) is
`packages/cb-api/tests/test_analytics_endpoints.py`.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi import HTTPException

from cb_api.routers.analytics import _DEFAULT_DAYS, _MAX_DAYS, resolve_window
from cb_core.analytics import DailyStats, summarise


def _row(day: date, **overrides: int | float | None) -> DailyStats:
    fields: dict[str, object] = {
        "day": day,
        "messages": 0,
        "commands": 0,
        "joins": 0,
        "leaves": 0,
        "captcha_issued": 0,
        "captcha_solved": 0,
        "active_users": 0,
        "errors": 0,
        "p95_latency_ms": None,
        "llm_tokens": 0,
        "llm_cost_usd": 0.0,
    }
    fields.update(overrides)
    return DailyStats(**fields)  # type: ignore[arg-type]


class TestResolveWindow:
    def test_no_dates_is_the_last_thirty_days_inclusive(self) -> None:
        start, end = resolve_window(None, None)
        assert end == datetime.now(UTC).date()
        assert (end - start).days + 1 == _DEFAULT_DAYS

    def test_only_a_start_means_thirty_days_from_it(self) -> None:
        """Not "everything since": an open-ended range is the unbounded list
        D11 exists to prevent."""
        start, end = resolve_window(date(2026, 1, 1), None)
        assert start == date(2026, 1, 1)
        assert end == date(2026, 1, 30)

    def test_only_an_end_means_thirty_days_up_to_it(self) -> None:
        start, end = resolve_window(None, date(2026, 3, 31))
        assert start == date(2026, 3, 2)
        assert end == date(2026, 3, 31)

    def test_both_dates_are_honoured(self) -> None:
        start, end = resolve_window(date(2026, 2, 1), date(2026, 2, 3))
        assert (start, end) == (date(2026, 2, 1), date(2026, 2, 3))

    def test_a_single_day_is_a_valid_window(self) -> None:
        day = date(2026, 2, 1)
        assert resolve_window(day, day) == (day, day)

    def test_a_reversed_range_is_a_400_not_a_silent_swap(self) -> None:
        with pytest.raises(HTTPException) as excinfo:
            resolve_window(date(2026, 2, 3), date(2026, 2, 1))
        assert excinfo.value.status_code == 400

    def test_a_window_wider_than_a_year_is_a_400(self) -> None:
        start = date(2025, 1, 1)
        with pytest.raises(HTTPException) as excinfo:
            resolve_window(start, start + timedelta(days=_MAX_DAYS))
        assert excinfo.value.status_code == 400

    def test_exactly_the_maximum_is_allowed(self) -> None:
        start = date(2025, 1, 1)
        end = start + timedelta(days=_MAX_DAYS - 1)
        assert resolve_window(start, end) == (start, end)


class TestSummarise:
    def test_totals_across_the_window(self) -> None:
        rows = (
            _row(date(2026, 1, 1), messages=10, commands=2, errors=1, llm_cost_usd=0.5),
            _row(date(2026, 1, 2), messages=5, commands=3, errors=0, llm_cost_usd=0.25),
        )
        summary = summarise(rows)
        assert summary["days"] == 2
        assert summary["messages"] == 15
        assert summary["commands"] == 5
        assert summary["errors"] == 1
        assert summary["llm_cost_usd"] == 0.75

    def test_active_users_is_the_peak_day_not_a_sum(self) -> None:
        """Summing daily actives would count the same person once per day and
        produce a number larger than the group."""
        rows = (_row(date(2026, 1, 1), active_users=30), _row(date(2026, 1, 2), active_users=12))
        assert summarise(rows)["peak_active_users"] == 30

    def test_latency_is_the_worst_day_not_an_average_of_percentiles(self) -> None:
        rows = (
            _row(date(2026, 1, 1), p95_latency_ms=120),
            _row(date(2026, 1, 2), p95_latency_ms=900),
        )
        assert summarise(rows)["worst_p95_latency_ms"] == 900

    def test_latency_is_none_when_no_day_recorded_one(self) -> None:
        assert summarise((_row(date(2026, 1, 1)),))["worst_p95_latency_ms"] is None

    def test_solve_rate_is_none_when_nobody_was_challenged(self) -> None:
        """ "nobody was asked" and "nobody solved it" are different facts; a
        dashboard must not draw them the same."""
        assert summarise((_row(date(2026, 1, 1)),))["captcha_solve_rate"] is None

    def test_solve_rate_is_a_ratio_when_someone_was(self) -> None:
        rows = (_row(date(2026, 1, 1), captcha_issued=4, captcha_solved=3),)
        assert summarise(rows)["captcha_solve_rate"] == 0.75

    def test_an_empty_window_summarises_to_zeroes_not_an_error(self) -> None:
        summary = summarise(())
        assert summary["days"] == 0
        assert summary["messages"] == 0
        assert summary["peak_active_users"] == 0
        assert summary["worst_p95_latency_ms"] is None
