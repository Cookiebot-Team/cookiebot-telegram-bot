"""`cb_core.audit` — the diff it stores, and what happens when the write fails.

The SQL itself is exercised against a real Citus in
`qa/integration/test_audit_log.py`; this layer covers the two decisions that
are not SQL: which fields a row quotes, and that a failed insert never turns a
change that already happened into an error the caller sees.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from cb_core import audit
from cb_core import db as db_mod

GROUP_ID = -1001234567890
ACTOR = 4242


def test_diff_keeps_only_what_changed() -> None:
    before, after = audit.diff(
        {"sfw": True, "language": "en", "max_posts": 3},
        {"sfw": False, "language": "en", "max_posts": 3},
    )
    assert before == {"sfw": True}
    assert after == {"sfw": False}


def test_diff_of_an_untouched_form_is_empty() -> None:
    """The caller uses this to decide whether to record at all — a save button
    pressed with nothing changed should leave no row."""
    same = {"sfw": True, "language": "pt"}
    assert audit.diff(same, dict(same)) == ({}, {})


def test_diff_reports_a_field_that_had_no_previous_value() -> None:
    before, after = audit.diff({}, {"thread_posts": "42"})
    assert before == {"thread_posts": None}
    assert after == {"thread_posts": "42"}


@pytest.mark.asyncio
async def test_record_writes_the_row_and_returns_it(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_execute(stmt: str, *args: Any, name: str = "") -> str:
        captured["stmt"] = stmt
        captured["args"] = args
        captured["name"] = name
        return "INSERT 0 1"

    monkeypatch.setattr(db_mod, "execute", fake_execute)

    event = await audit.record(
        GROUP_ID,
        audit.CONFIG_UPDATED,
        actor_user_id=ACTOR,
        surface="miniapp",
        summary="changed sfw",
        before={"sfw": True},
        after={"sfw": False},
    )

    assert event is not None
    assert event.id.version == 7  # sortable by creation time, no second index
    assert captured["args"][0] == GROUP_ID  # the shard key leads
    assert captured["name"] == "audit_insert"
    # jsonb goes over the wire as text; asyncpg does not encode dicts itself.
    assert json.loads(captured["args"][9]) == {"sfw": False}


@pytest.mark.asyncio
async def test_a_failed_write_is_swallowed_and_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The action being audited already happened. Raising here would tell the
    client their change did not land when it did."""

    async def boom(stmt: str, *args: Any, name: str = "") -> str:
        raise RuntimeError("no route to host")

    monkeypatch.setattr(db_mod, "execute", boom)
    before = _counter_value("cb_audit_write_failures_total", action=audit.CONFIG_UPDATED)

    assert await audit.record(GROUP_ID, audit.CONFIG_UPDATED) is None

    after = _counter_value("cb_audit_write_failures_total", action=audit.CONFIG_UPDATED)
    assert after == before + 1


@pytest.mark.asyncio
async def test_page_passes_the_keyset_and_filters_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_fetch(stmt: str, *args: Any, name: str = "") -> list[Any]:
        captured["stmt"] = stmt
        captured["args"] = args
        return []

    monkeypatch.setattr(db_mod, "fetch", fake_fetch)

    await audit.page(GROUP_ID, limit=25, action=audit.RULES_UPDATED, actor_user_id=ACTOR)

    assert captured["args"] == (GROUP_ID, None, audit.RULES_UPDATED, ACTOR, 25)
    assert "ORDER BY id DESC" in captured["stmt"]
    assert "OFFSET" not in captured["stmt"]  # D11: keyset, never offset


def _counter_value(metric: str, **labels: str) -> float:
    from prometheus_client import REGISTRY

    value = REGISTRY.get_sample_value(f"{metric}_total", labels) or REGISTRY.get_sample_value(
        metric, labels
    )
    return value or 0.0
