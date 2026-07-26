"""Unit tests for the per-update span TelemetryMiddleware opens and for the
outcome it carries — no Telegram session, no database, no tracer provider
touched process-wide (`spans` below patches only the one function
`cb_core.telemetry.span()` calls to get a tracer, so this cannot leak into any
other test's global OpenTelemetry state).

Model: `test_config_menu.py` (pure transformations, no infra) plus
`test_transport.py` (drives a real aiogram object without a live bot process).
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from aiogram.types import Update
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import cb_core.telemetry as cb_telemetry
from cb_gateway.middlewares import TelemetryMiddleware, _callback_action, _interaction_name
from cb_gateway.telemetry import OUTCOME_ATTR, mark_outcome

GROUP_ID = -1001234567890
USER_ID = 555001
_DATA = {"skin": "cookiebot", "bot_username": "CookieMWbot"}


@pytest.fixture
def spans(monkeypatch: pytest.MonkeyPatch) -> InMemorySpanExporter:
    """A private `TracerProvider` in place of the process-wide one.

    A real `TracerProvider` can only ever be installed once per process (the
    OTel spec enforces it), so a test cannot call `trace.set_tracer_provider`
    without risking a later test — or a later run of this one under `-p
    randomly` — silently keeping whatever the first caller installed.
    `cb_core.telemetry.span()` always resolves its tracer through
    `trace.get_tracer`; patching that one call point gets every span this test
    opens without touching anything global.
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(cb_telemetry.trace, "get_tracer", lambda name: provider.get_tracer(name))
    return exporter


def _message_update(text: str, *, update_id: int = 1) -> Update:
    payload: dict[str, Any] = {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": int(time.time()),
            "chat": {"id": GROUP_ID, "type": "supergroup", "title": "unit"},
            "from": {"id": USER_ID, "is_bot": False, "first_name": "Test"},
            "text": text,
            "entities": (
                [{"offset": 0, "length": len(text.split(" ")[0]), "type": "bot_command"}]
                if text.startswith("/")
                else []
            ),
        },
    }
    return Update.model_validate(payload)


def _callback_update(data: str, *, update_id: int = 1) -> Update:
    payload: dict[str, Any] = {
        "update_id": update_id,
        "callback_query": {
            "id": str(update_id),
            "from": {"id": USER_ID, "is_bot": False, "first_name": "Test"},
            "chat_instance": str(GROUP_ID),
            "data": data,
            "message": {
                "message_id": update_id,
                "date": int(time.time()),
                "chat": {"id": GROUP_ID, "type": "supergroup", "title": "unit"},
                "from": {
                    "id": 424242,
                    "is_bot": True,
                    "first_name": "Bot",
                    "username": "CookieMWbot",
                },
                "text": "menu",
            },
        },
    }
    return Update.model_validate(payload)


def _join_update(*, update_id: int = 1) -> Update:
    payload: dict[str, Any] = {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": int(time.time()),
            "chat": {"id": GROUP_ID, "type": "supergroup", "title": "unit"},
            "from": {"id": USER_ID, "is_bot": False, "first_name": "Test"},
            "new_chat_members": [{"id": USER_ID + 1, "is_bot": False, "first_name": "Newcomer"}],
        },
    }
    return Update.model_validate(payload)


def _leave_update(*, update_id: int = 1) -> Update:
    payload: dict[str, Any] = {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": int(time.time()),
            "chat": {"id": GROUP_ID, "type": "supergroup", "title": "unit"},
            "from": {"id": USER_ID, "is_bot": False, "first_name": "Test"},
            "left_chat_member": {"id": USER_ID + 1, "is_bot": False, "first_name": "Newcomer"},
        },
    }
    return Update.model_validate(payload)


async def _noop_handler(event: Any, data: dict[str, Any]) -> None:
    return None


def _only_span(exporter: InMemorySpanExporter) -> ReadableSpan:
    finished = exporter.get_finished_spans()
    assert len(finished) == 1, finished
    return finished[0]


# --------------------------------------------------------- naming, no I/O


class TestInteractionName:
    def test_command_names_after_the_resolved_command(self) -> None:
        update = _message_update("/rules")
        assert _interaction_name(update, "rules") == "telegram.command /rules"

    def test_plain_message_has_no_interaction_name(self) -> None:
        """Falls through to the generic `telegram.update.<type>` name in the
        middleware — a passive content-rule handler's update, not a command."""
        update = _message_update("just chatting")
        assert _interaction_name(update, None) is None

    def test_callback_action_keeps_the_wire_shape_markers(self) -> None:
        assert _callback_action("k CONFIG 123456") == "k:config"
        assert _callback_action("CALLADMS YES 987") == "calladms:yes"

    def test_callback_action_never_leaks_the_bare_id(self) -> None:
        assert "123456" not in _callback_action("k CONFIG 123456")

    def test_callback_action_handles_missing_data(self) -> None:
        assert _callback_action(None) == "unknown"
        assert _callback_action("") == "unknown"

    def test_callback_query_is_named_by_its_action(self) -> None:
        update = _callback_update("k CONFIG 123456")
        assert _interaction_name(update, None) == "telegram.callback k:config"

    def test_bot_join_is_named_member_join(self) -> None:
        assert _interaction_name(_join_update(), None) == "telegram.member_join"

    def test_member_leave_is_named_member_leave(self) -> None:
        assert _interaction_name(_leave_update(), None) == "telegram.member_leave"


# ------------------------------------------------------------- middleware


class TestTelemetryMiddlewareSpans:
    async def test_command_span_defaults_to_answered(self, spans: InMemorySpanExporter) -> None:
        update = _message_update("/rules")
        await TelemetryMiddleware()(_noop_handler, update, dict(_DATA))
        finished = _only_span(spans)
        assert finished.name == "telegram.command /rules"
        assert finished.attributes is not None
        assert finished.attributes[OUTCOME_ATTR] == "answered"

    async def test_handler_can_override_the_default_to_silent(
        self, spans: InMemorySpanExporter
    ) -> None:
        async def handler(event: Any, data: dict[str, Any]) -> None:
            # Exactly what listcommand.py / rules.py do at their own silent
            # branches: mark, then return without replying.
            mark_outcome("silent")

        update = _message_update("/rules")
        await TelemetryMiddleware()(handler, update, dict(_DATA))
        finished = _only_span(spans)
        assert finished.attributes is not None
        assert finished.attributes[OUTCOME_ATTR] == "silent"

    async def test_handler_exception_is_recorded_and_still_propagates(
        self, spans: InMemorySpanExporter
    ) -> None:
        async def handler(event: Any, data: dict[str, Any]) -> None:
            raise RuntimeError("boom")

        update = _message_update("/rules")
        with pytest.raises(RuntimeError, match="boom"):
            await TelemetryMiddleware()(handler, update, dict(_DATA))

        finished = _only_span(spans)
        assert finished.attributes is not None
        assert finished.attributes[OUTCOME_ATTR] == "error"
        assert finished.status.status_code.name == "ERROR"
        assert any(event.name == "exception" for event in finished.events)

    async def test_passive_message_gets_no_outcome_default(
        self, spans: InMemorySpanExporter
    ) -> None:
        """Not every update is a command/interaction — a plain message headed
        for embedder/mediarestrict/fun_random gets the old generic span name and
        no `cb.outcome` guess (see `middlewares.py`'s `_interaction_name`)."""
        update = _message_update("just chatting")
        await TelemetryMiddleware()(_noop_handler, update, dict(_DATA))
        finished = _only_span(spans)
        assert finished.name == "telegram.update.message"
        assert finished.attributes is not None
        assert OUTCOME_ATTR not in finished.attributes
