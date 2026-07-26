"""Unit coverage for util_embedder — pure logic only, no dispatcher, no
Telegram, no DB.

See docs/contracts/util_embedder.md for the full behaviour contract and
qa/features/util_embedder.feature + qa/test_util_embedder.py for the
end-to-end version of the same assertions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.types import LinkPreviewOptions

from cb_gateway.handlers import embedder


@dataclass
class _FakeUser:
    id: int = 555001


@dataclass
class _FakeMessage:
    """Only the attributes the handler actually reads."""

    text: str | None = None
    from_user: _FakeUser | None = field(default_factory=_FakeUser)
    chat: Any = field(default_factory=lambda: type("Chat", (), {"id": -100})())
    reply: Any = None


_DEFAULT_USER = _FakeUser()


def _message(text: str | None, *, from_user: _FakeUser | None = _DEFAULT_USER) -> _FakeMessage:
    message = _FakeMessage(text=text, from_user=from_user)
    message.reply = AsyncMock()
    return message


# ---------------------------------------------------------------- rewritten_links


class TestRewrittenLinks:
    def test_twitter_status_link(self) -> None:
        """`SocialContent.py:58` - `(?:twitter|x)\\.com/.../status/\\d+` -> `fixupx.com`."""
        text = "https://x.com/someuser/status/1234567890123"
        assert embedder.rewritten_links(text) == [
            "https://fixupx.com/someuser/status/1234567890123"
        ]

    def test_twitter_dot_com_form_also_matches(self) -> None:
        text = "https://twitter.com/someuser/status/42"
        assert embedder.rewritten_links(text) == ["https://fixupx.com/someuser/status/42"]

    def test_tiktok_video_link(self) -> None:
        """`SocialContent.py:59` - long-form `tiktok.com/@user/video/id` -> `vm.vxtiktok.com`."""
        text = "https://www.tiktok.com/@someuser/video/7123456789012345678"
        assert embedder.rewritten_links(text) == [
            "https://vm.vxtiktok.com/@someuser/video/7123456789012345678"
        ]

    def test_bluesky_profile_link(self) -> None:
        """`SocialContent.py:61` - `bsky.app/profile/...` -> `fxbsky.app`."""
        text = "https://bsky.app/profile/alice.bsky.social/post/3jt6vw"
        assert embedder.rewritten_links(text) == [
            "https://fxbsky.app/profile/alice.bsky.social/post/3jt6vw"
        ]

    def test_instagram_is_detected_but_never_rewritten(self) -> None:
        """v1's Instagram transformation is present in source but commented out
        (`SocialContent.py:60,71-74`) - today's v1 does not rewrite it, so
        neither does this port, even though `find_embeddable_links` detects
        the host."""
        text = "https://instagram.com/p/Cabc123XYZ/"
        assert embedder.rewritten_links(text) == []

    @pytest.mark.parametrize(
        "text",
        [
            "https://reddit.com/r/aww/comments/abc123/cute_dog",
            "https://pixiv.net/en/artworks/12345",
            "https://e621.net/posts/12345",
            "https://furaffinity.net/view/12345",
        ],
    )
    def test_hosts_with_no_v1_target_are_never_rewritten(self, text: str) -> None:
        """These hosts have no v1 equivalent at all - never appear anywhere in
        `SocialContent.py`. `find_embeddable_links` detects them as
        "embeddable" (`cb_core/textmatch.py`, not owned by this port), but this
        handler does not invent a target domain nobody has verified."""
        assert embedder.rewritten_links(text) == []

    def test_unsupported_domain_is_ignored(self) -> None:
        assert embedder.rewritten_links("https://example.com/cool-article") == []

    def test_no_links_at_all(self) -> None:
        assert embedder.rewritten_links("just chatting, nothing here") == []

    def test_multiple_links_preserve_message_order(self) -> None:
        text = (
            "look at https://x.com/someuser/status/1 "
            "and also https://bsky.app/profile/bob.bsky.social/post/xyz"
        )
        assert embedder.rewritten_links(text) == [
            "https://fixupx.com/someuser/status/1",
            "https://fxbsky.app/profile/bob.bsky.social/post/xyz",
        ]

    def test_link_embedded_in_a_sentence_is_still_found(self) -> None:
        """Deliberate divergence from v1's `requests.get(message, timeout=2)`
        "validation" (`SocialContent.py:54`), which raises for anything that
        isn't a bare, complete URL and so in practice only ever fired when the
        whole message was nothing but the link - see docs/contracts/util_embedder.md.
        `find_embeddable_links` is already built to find a link anywhere in
        the text; this port relies on that rather than reproducing the bug."""
        text = "check this out https://bsky.app/profile/alice.bsky.social/post/3jt6vw please"
        assert embedder.rewritten_links(text) == [
            "https://fxbsky.app/profile/alice.bsky.social/post/3jt6vw"
        ]

    def test_already_fixed_domain_is_not_detected(self) -> None:
        """v1's explicit guard against re-rewriting an already-embedded link
        (`SocialContent.py:51`) is redundant here only because `fixupx.com`
        etc. are not in `find_embeddable_links`'s host list to begin with -
        same observable result (no rewrite), different mechanism."""
        assert embedder.rewritten_links("https://fixupx.com/someuser/status/42") == []


# -------------------------------------------------------------------- handler


@pytest.mark.asyncio
async def test_command_text_is_never_touched() -> None:
    """v1 never reaches `check_reply_embed` for a command: the whole
    `msg['text'].startswith('/')` branch (`COOKIEBOT.py:186`) is a sibling of
    the trailing `else` this feature lives in, not a parent of it."""
    message = _message("/somecommand https://bsky.app/profile/alice.bsky.social/post/3jt6vw")

    with pytest.raises(SkipHandler):
        await embedder.rewrite_embeddable_links(message)

    message.reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_sender_is_skipped() -> None:
    """v1's call site requires `'from' in msg` (`COOKIEBOT.py:310-312`)."""
    message = _message("https://bsky.app/profile/alice.bsky.social/post/3jt6vw", from_user=None)

    with pytest.raises(SkipHandler):
        await embedder.rewrite_embeddable_links(message)

    message.reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_embeddable_link_is_skipped() -> None:
    message = _message("just chatting, nothing here")

    with pytest.raises(SkipHandler):
        await embedder.rewrite_embeddable_links(message)

    message.reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_replies_with_the_rewritten_link() -> None:
    message = _message("https://x.com/someuser/status/1234567890123")

    await embedder.rewrite_embeddable_links(message)

    message.reply.assert_awaited_once()
    (text,), kwargs = message.reply.await_args
    assert text == "https://fixupx.com/someuser/status/1234567890123"
    options = kwargs["link_preview_options"]
    assert isinstance(options, LinkPreviewOptions)
    assert options.show_above_text is True
    assert options.prefer_large_media is True
    assert options.is_disabled is False


@pytest.mark.asyncio
async def test_replies_with_every_rewritable_link_on_its_own_line() -> None:
    message = _message(
        "look at https://x.com/someuser/status/1 "
        "and https://bsky.app/profile/bob.bsky.social/post/xyz"
    )

    await embedder.rewrite_embeddable_links(message)

    (text,), _ = message.reply.await_args
    assert text == (
        "https://fixupx.com/someuser/status/1\nhttps://fxbsky.app/profile/bob.bsky.social/post/xyz"
    )


@pytest.mark.asyncio
async def test_instagram_only_message_produces_no_reply() -> None:
    """The link is detected by `find_embeddable_links` but has no v1 target -
    the handler must still skip cleanly, not reply with an empty string."""
    message = _message("https://instagram.com/p/Cabc123XYZ/")

    with pytest.raises(SkipHandler):
        await embedder.rewrite_embeddable_links(message)

    message.reply.assert_not_awaited()
