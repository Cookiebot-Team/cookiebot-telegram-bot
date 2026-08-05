"""Unit coverage for `cb_gateway.handlers.publisher`'s pure surface.

Triggers, the callback wire and `/repost`'s argument parsing. The flows that
need a dispatcher, a database or Telegram are covered in
`qa/test_util_postforwarder.py`; see `docs/contracts/util_postforwarder.md`.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from cb_gateway.filters import CommandName
from cb_gateway.handlers import publisher


@dataclass
class _FakeMessage:
    text: str | None


# ---------------------------------------------------------------------- triggers


@pytest.mark.parametrize("spelling", ["publish", "divulgar", "publicar"])
@pytest.mark.asyncio
async def test_every_v1_publish_spelling_resolves(spelling: str) -> None:
    """`COOKIEBOT.py:205`. AGENTS.md §2.1: dropping one is the one thing
    forbidden."""
    result = await CommandName("publish")(_FakeMessage(f"/{spelling}"), bot_username="CookieMWbot")
    assert result is not False


@pytest.mark.parametrize("spelling", ["repost", "repostar", "reenviar"])
@pytest.mark.asyncio
async def test_every_v1_repost_spelling_resolves(spelling: str) -> None:
    """`COOKIEBOT.py:207`."""
    result = await CommandName("repost")(_FakeMessage(f"/{spelling} 3"), bot_username="CookieMWbot")
    assert result is not False


# ------------------------------------------------------------------ callback wire


def test_submit_round_trip() -> None:
    markup = publisher.build_approval_request(
        origin_chat_id=-100123, chat_id=-100456, forward_from_message_id=77, message_id=88
    )
    data = markup.inline_keyboard[0][0].callback_data or ""
    assert publisher.parse_submit(data) == (-100123, -100456, 77, 88)


def test_the_group_prompts_deny_button_carries_no_id() -> None:
    """v1 `:52` emits a bare `nPub`, and `deny_post` returns early on a
    one-field payload (`:224-225`) — so that button deletes the prompt and
    evicts nothing. Preserved deliberately."""
    markup = publisher.build_approval_request(
        origin_chat_id=1, chat_id=2, forward_from_message_id=3, message_id=4
    )
    assert markup.inline_keyboard[1][0].callback_data == "nPub"
    assert publisher.parse_deny("nPub") is None


def test_approval_keyboard_has_v1s_five_buttons_in_order() -> None:
    """`:86-91`."""
    labels = [label for label, _, _ in publisher.APPROVAL_OPTIONS]
    assert labels == ["✔️ 7 days (NSFW)", "✔️ 7 days", "✔️ 3 days", "✔️ 1 day"]


def test_approve_payload_round_trip_carries_the_nsfw_flag() -> None:
    data = "yPub -100123 -100456 77 999 7 88 1"
    assert publisher.parse_approve(data) == (-100123, -100456, 77, 999, 7, 88, True)


def test_approve_payload_without_the_nsfw_flag_set() -> None:
    assert publisher.parse_approve("yPub 1 2 3 4 7 5 0") == (1, 2, 3, 4, 7, 5, False)


@pytest.mark.parametrize(
    "data",
    [
        "yPub 1 2 3 4 7 5",  # too few fields
        "yPub 1 2 3 4 7 5 0 6",  # too many — v1's split()[:8] would accept this
        "yPub a 2 3 4 7 5 0",  # non-numeric id
        "yPub 1 2 3 4 7 5 2",  # nsfw flag outside {0,1}
        "nPub 1 2 3 4 7 5 0",  # wrong token
        "",
    ],
)
def test_malformed_approve_payloads_are_rejected(data: str) -> None:
    assert publisher.parse_approve(data) is None


def test_approval_chat_deny_payload_carries_the_id() -> None:
    """`:90` — this one *does* evict the cache entry."""
    assert publisher.parse_deny("nPub 77") == 77


# ------------------------------------------------------------- registration order


def test_the_reply_relay_is_registered_where_v1s_elif_sits() -> None:
    """v1 runs `check_notify_post_reply` from an `elif` after the captcha-reply
    and complaint-reply checks and *before* the conversational-AI branch
    (`COOKIEBOT.py:296-303`).

    Registered after `chat_ai`, a reply to a published post is answered by the
    AI instead of reaching the post's author — and nothing errors, which is why
    this is a test and not a comment. Read off the source: the routers are
    module-level singletons, so calling `build_router()` twice in one
    interpreter raises `RuntimeError: Router is already attached`.
    """
    import inspect

    from cb_gateway.handlers import build_router

    source = inspect.getsource(build_router)
    relay = source.index("include_router(publisher.relay_router)")
    assert source.index("include_router(groupguardian.router)") < relay
    assert source.index("include_router(complaint.router)") < relay
    assert relay < source.index("include_router(chat_ai.router)")


# --------------------------------------------------------------------- /repost args


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ("", publisher.REPOST_UNLIMITED_DAYS),  # v1 `:306`
        ("7", 7),
        ("7 extra", 7),  # v1 tests only the second word
        ("0", 0),
        ("abc", None),  # v1 `:299-302`
        ("-1", None),  # "-1".isnumeric() is False in v1 too
        ("3.5", None),
    ],
)
def test_parse_repost_days(args: str, expected: int | None) -> None:
    assert publisher.parse_repost_days(args) == expected


def test_repost_uses_v1s_daytime_window() -> None:
    """`:310-311`: hour 10-17, unlike the fan-out's 0-23 (`:268-269`)."""
    for _ in range(200):
        hour, minute = publisher.repost_schedule_time()
        assert 10 <= hour <= 17
        assert 0 <= minute <= 59
