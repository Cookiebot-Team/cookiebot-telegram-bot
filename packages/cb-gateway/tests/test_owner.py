"""Unit coverage for `cb_gateway.handlers.owner`'s pure surface.

The owner gate, the argument parser and the group listing. End-to-end
behaviour is `qa/test_x_owner_commands.py`; the fan-out is
`packages/cb-worker/tests/test_broadcast_job.py`.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from cb_core.ops import GroupSummary
from cb_gateway.filters import CommandName
from cb_gateway.handlers.owner import format_group_list, is_owner, parse_subject


@dataclass
class _FakeMessage:
    text: str | None


@pytest.mark.parametrize(
    ("alias", "canonical"),
    [
        ("/grupos", "groups"),
        ("/groups", "groups"),
        ("/leave", "leave"),
        ("/blacklist", "blacklist"),
        ("/unblacklist", "unblacklist"),
        ("/broadcast", "broadcast"),
        ("/stop", "stop"),
        ("/restart", "restart"),
    ],
)
@pytest.mark.asyncio
async def test_every_v1_owner_trigger_resolves(alias: str, canonical: str) -> None:
    result = await CommandName(canonical)(_FakeMessage(alias), bot_username="CookieMWbot")
    assert result is not False


def test_nobody_is_the_owner_when_it_is_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    """v1's `int(os.getenv('ownerID'))` crashes at import when unset, so there
    is no "unconfigured means everyone" behaviour to preserve — and defaulting
    the other way would hand `/broadcast` to anyone."""
    from cb_core.settings import Settings
    from cb_gateway.handlers import owner

    monkeypatch.setattr(owner, "get_settings", lambda: Settings(owner_id=0))
    assert not is_owner(0)
    assert not is_owner(12345)


def test_only_the_configured_owner_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    from cb_core.settings import Settings
    from cb_gateway.handlers import owner

    monkeypatch.setattr(owner, "get_settings", lambda: Settings(owner_id=999))
    assert is_owner(999)
    assert not is_owner(998)


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ("424243", 424243),
        # v1 strips a leading "@" (`universal_funcs.py:307-308`) — which never
        # made a username work, it just made "@123" and "123" the same id.
        ("@424243", 424243),
        ("424243 and some noise", 424243),
        ("-1001234567890", -1001234567890),
        ("", None),
        ("@someuser", None),
    ],
)
def test_subject_parsing_matches_v1s(args: str, expected: int | None) -> None:
    assert parse_subject(args) == expected


def test_group_list_is_one_message_with_a_total() -> None:
    """v1 sent one Telegram message per group plus a 0.4s getChat between each
    (FEATURE-MAP D11)."""
    body = format_group_list((GroupSummary(-100, "First"), GroupSummary(-200, "Second")), total=2)
    assert body.splitlines() == ["-100 - First", "-200 - Second", "Total groups found: 2"]


def test_the_total_is_the_real_count_not_the_page_size() -> None:
    body = format_group_list((GroupSummary(-100, "Only one shown"),), total=417)
    assert body.endswith("Total groups found: 417")
