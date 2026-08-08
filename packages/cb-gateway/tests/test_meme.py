"""Unit coverage for `cb_gateway.handlers.meme`'s reply-path surface.

The trigger and the one refusal v1 makes before touching anything. Selection
is `packages/cb-core/tests/test_meme_templates.py`; the composite is
`packages/cb-worker/tests/test_meme_job.py`.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from cb_gateway.filters import CommandName
from cb_gateway.handlers.battle import parse_tagged_targets
from cb_gateway.handlers.meme import MAX_TAGGED


@dataclass
class _FakeMessage:
    text: str | None


@pytest.mark.asyncio
async def test_meme_resolves() -> None:
    result = await CommandName("meme")(_FakeMessage("/meme"), bot_username="CookieMWbot")
    assert result is not False


@pytest.mark.asyncio
async def test_addressed_at_a_different_bot_does_not_resolve() -> None:
    result = await CommandName("meme")(
        _FakeMessage("/meme@SomeOtherBot"), bot_username="CookieMWbot"
    )
    assert result is False


def test_the_cap_matches_the_largest_template() -> None:
    """v1 refuses more than five (`SocialContent.py:230`), which is also the
    largest `blob_count` any template has (`metadata.py:8`)."""
    from cb_core.meme_templates import MAX_BLOBS

    assert MAX_TAGGED == MAX_BLOBS


def test_six_tags_is_over_the_cap_and_five_is_not() -> None:
    assert len(parse_tagged_targets("/meme @a @b @c @d @e @f")) > MAX_TAGGED
    assert len(parse_tagged_targets("/meme @a @b @c @d @e")) <= MAX_TAGGED
