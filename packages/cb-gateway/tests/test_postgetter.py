"""Unit coverage for `cb_gateway.handlers.postgetter`.

The filter reproduces a six-way conjunction from v1's dispatcher
(`COOKIEBOT.py:165`), and getting any one arm wrong means either every media
message gets a "Share post?" prompt or no ad ever does. One negative case per
arm, plus the registration-order guarantee that makes the whole thing a port
rather than a new feature. Contract: `docs/contracts/util_postgetter.md`.
"""

from __future__ import annotations

import pytest

from cb_core import locales
from cb_gateway.handlers import postgetter


def test_es_is_prompted_in_english() -> None:
    """D-PG-3, preserved: v1's ternary is `pt` or English, with no Spanish arm
    at all (`Publisher.py:48`). Expressed by leaving the key out of the `es`
    catalog rather than duplicating the English string into it, so the omission
    stays visible to anyone diffing the catalogs."""
    assert locales.get("publisher_ask_prompt", "en") == "Share post?"
    assert locales.get("publisher_ask_prompt", "pt") == "Divulgar postagem?"
    assert locales.get("publisher_ask_prompt", "es") == "Share post?"
    assert "publisher_ask_prompt" not in locales.catalog("es")


def test_the_prompt_carries_v1s_two_buttons() -> None:
    """`Publisher.py:50-54`, via the wire `util_postforwarder` owns."""
    markup = postgetter.build_approval_request(
        origin_chat_id=-100999, chat_id=-100111, forward_from_message_id=42, message_id=7
    )
    assert [row[0].text for row in markup.inline_keyboard] == ["✔️", "❌"]
    assert markup.inline_keyboard[0][0].callback_data == "SendToApprovalPub -100999 -100111 42 7"


def test_registered_ahead_of_fun_random() -> None:
    """v1's branch is an `elif` that precedes the random-library branches
    (`COOKIEBOT.py:165-172`), so an auto-forwarded ad is never also pooled.

    aiogram reproduces that only through registration order, and getting it
    wrong is silent — hence an assertion rather than a comment.
    """
    from cb_gateway.handlers import build_router

    names = [r.name for r in build_router().sub_routers]
    assert names.index("postgetter") < names.index("fun_random")


class _FakeUser:
    def __init__(self, first_name: str) -> None:
        self.first_name = first_name


class _FakeMessage:
    def __init__(self, first_name: str | None, forward_from_message_id: int | None) -> None:
        self.from_user = _FakeUser(first_name) if first_name is not None else None
        self.forward_from_message_id = forward_from_message_id


@pytest.mark.parametrize(
    ("first_name", "forward_id", "expected"),
    [
        ("Telegram", 42, True),
        # v1 compares the literal string (`COOKIEBOT.py:165`); a human forwarding
        # the same post by hand is not the trigger.
        ("Ana", 42, False),
        ("telegram", 42, False),
        ("Telegram", None, False),
        (None, 42, False),
    ],
)
def test_the_auto_forward_discriminator(
    first_name: str | None, forward_id: int | None, expected: bool
) -> None:
    message = _FakeMessage(first_name, forward_id)
    assert postgetter._is_auto_forwarded_ad(message) is expected  # type: ignore[arg-type] # noqa: SLF001 - the predicate is the unit
