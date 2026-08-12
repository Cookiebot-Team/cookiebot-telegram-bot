"""Unit coverage for x_sticker_autoreply — pure logic and handler
orchestration, no dispatcher, no real Telegram, no DB.

See `cb_gateway.handlers.sticker_autoreply`'s own module docstring for the
full v1 behaviour table and `qa/features/x_sticker_autoreply.feature` +
`qa/test_x_sticker_autoreply.py` for the end-to-end version against a real
database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.types import Message

from cb_core.group_config import GroupConfig
from cb_gateway.handlers import sticker_autoreply


@dataclass
class _FakeUser:
    id: int
    username: str | None = None


@dataclass
class _FakeSticker:
    file_id: str = "sticker-file-1"
    emoji: str | None = "\U0001f600"  # 😀, not in the banned list
    set_name: str | None = "CleanPack1"


@dataclass
class _FakeChat:
    id: int = -100
    title: str | None = "QA Group"


@dataclass
class _FakeMessage:
    """Only the attributes the handlers actually read."""

    from_user: _FakeUser | None = field(default_factory=lambda: _FakeUser(id=1, username="tester"))
    sticker: Any = None
    document: Any = None
    animation: Any = None
    reply_to_message: Any = None
    chat: Any = field(default_factory=_FakeChat)
    bot: Any = None
    reply_sticker: Any = field(default_factory=AsyncMock)


class _FakeConfig:
    def __init__(self, *, sfw: bool = True, functions_fun: bool = True) -> None:
        self.sfw = sfw
        self.functions_fun = functions_fun


class _FakeCtx:
    def __init__(self, *, group_id: int = -100, config: _FakeConfig | None = None) -> None:
        self.group_id = group_id
        self.lang = "en"
        self.config = config or _FakeConfig()
        self._fun_enabled = self.config.functions_fun

    def enabled(self, area: str) -> bool:
        assert area == "fun"
        return self._fun_enabled


class _FakeBot:
    def __init__(self, bot_id: int = 424242) -> None:
        self.id = bot_id


def _msg(message: _FakeMessage) -> Message:
    return cast(Message, message)


def _cfg(config: _FakeConfig) -> GroupConfig:
    return cast(GroupConfig, config)


# ------------------------------------------------------------------ _has_nsfw_title


class TestHasNsfwTitle:
    """`SocialContent.py:194,211`'s `BANNED_TITLESUBSTRINGS` check — same list
    `fun_random._has_nsfw_title` uses, both write sides share it."""

    @pytest.mark.parametrize(
        "title",
        [
            "Yiff Group",
            "PORN paradise",
            "18+ only",
            "+18 chat",
            "nsfw stuff",
            "Hentai Fanclub",
            "rule34 posts",
            "r34 dump",
            "nude pics",
            "\U0001f51e restricted",
        ],
    )
    def test_flags_every_v1_marker_case_insensitively(self, title: str) -> None:
        assert sticker_autoreply._has_nsfw_title(title)  # noqa: SLF001

    def test_a_clean_title_is_not_flagged(self) -> None:
        assert not sticker_autoreply._has_nsfw_title("Sticker Fans")  # noqa: SLF001

    def test_none_title_is_not_flagged(self) -> None:
        assert not sticker_autoreply._has_nsfw_title(None)  # noqa: SLF001

    def test_empty_title_is_not_flagged(self) -> None:
        assert not sticker_autoreply._has_nsfw_title("")  # noqa: SLF001


# ------------------------------------------------------------------ _is_banned_emoji


class TestIsBannedEmoji:
    """`any(x in msg['sticker']['emoji'] for x in BANNED_EMOJIS)`
    (`SocialContent.py:215`) — substring containment, not equality."""

    @pytest.mark.parametrize(
        "emoji",
        ["\U0001f346", "\U0001f4a6", "\U0001f60f", "\U0001f525", "\U0001f44c\U0001f3fb"],
    )
    def test_every_v1_banned_emoji_is_flagged(self, emoji: str) -> None:
        assert sticker_autoreply._is_banned_emoji(emoji)  # noqa: SLF001

    def test_a_banned_emoji_list_has_forty_two_entries(self) -> None:
        # SocialContent.py:210's literal list, transcribed verbatim — this
        # count is the cheapest possible proof nothing was silently dropped
        # or duplicated away during transcription.
        assert len(sticker_autoreply._BANNED_EMOJIS) == 42  # noqa: SLF001

    def test_an_unrelated_emoji_is_not_flagged(self) -> None:
        assert not sticker_autoreply._is_banned_emoji("\U0001f600")  # noqa: SLF001 - 😀

    def test_containment_matches_a_banned_emoji_inside_a_longer_string(self) -> None:
        """v1's own check is substring, not equality — a multi-codepoint
        `emoji` field containing a banned one anywhere still matches."""
        assert sticker_autoreply._is_banned_emoji("prefix\U0001f346suffix")  # noqa: SLF001


# -------------------------------------------------------------------- _valid_set_name


class TestValidSetName:
    """`re.match(r'^[a-zA-Z0-9]+$', msg['sticker']['set_name'])` (`:216`)."""

    @pytest.mark.parametrize("name", ["CleanPack1", "abc123", "ABCDEF", "1"])
    def test_alphanumeric_names_are_valid(self, name: str) -> None:
        assert sticker_autoreply._valid_set_name(name)  # noqa: SLF001

    @pytest.mark.parametrize(
        "name", ["has space", "has-dash", "has_underscore", "emoji\U0001f346", ""]
    )
    def test_non_alphanumeric_names_are_invalid(self, name: str) -> None:
        assert not sticker_autoreply._valid_set_name(name)  # noqa: SLF001


# ------------------------------------------------------------------- _is_reply_to_bot


class TestIsReplyToBot:
    """Deviation 1: `reply.from_user.id == bot_id`, not v1's literal
    `first_name == 'Cookiebot'`."""

    def test_no_reply_is_not_a_reply_to_the_bot(self) -> None:
        message = _FakeMessage(reply_to_message=None)
        assert not sticker_autoreply._is_reply_to_bot(_msg(message), 424242)  # noqa: SLF001

    def test_reply_to_a_message_with_no_sender_is_not_a_reply_to_the_bot(self) -> None:
        reply = _FakeMessage(from_user=None)
        message = _FakeMessage(reply_to_message=reply)
        assert not sticker_autoreply._is_reply_to_bot(_msg(message), 424242)  # noqa: SLF001

    def test_reply_to_the_bots_own_id_matches(self) -> None:
        reply = _FakeMessage(from_user=_FakeUser(id=424242, username="CookieMWbot"))
        message = _FakeMessage(reply_to_message=reply)
        assert sticker_autoreply._is_reply_to_bot(_msg(message), 424242)  # noqa: SLF001

    def test_reply_to_a_persona_named_cookiebot_but_a_different_id_does_not_match(self) -> None:
        """The exact bug this deviation fixes: v1 matched on the literal
        first_name string, which is wrong for every skin whose persona is not
        named 'Cookiebot' (bombot, pawstralbot, ...). Any other user id --
        even one that happens to be *displayed* as 'Cookiebot' -- must not
        trigger the reply."""
        reply = _FakeMessage(from_user=_FakeUser(id=999999, username="SomeoneElse"))
        message = _FakeMessage(reply_to_message=reply)
        assert not sticker_autoreply._is_reply_to_bot(_msg(message), 424242)  # noqa: SLF001

    def test_reply_to_a_different_bot_persona_id_does_not_match(self) -> None:
        """bombot/pawstralbot's own replies must not trigger *this* bot's
        read side either -- the deviation this fixes cuts both ways."""
        reply = _FakeMessage(from_user=_FakeUser(id=555555))
        message = _FakeMessage(reply_to_message=reply)
        assert not sticker_autoreply._is_reply_to_bot(_msg(message), 424242)  # noqa: SLF001


# --------------------------------------------------------------- _should_pool_sticker


class TestShouldPoolSticker:
    """`if sfw and 'username' in msg['from']: add_to_sticker_database(msg)`
    (`COOKIEBOT.py:180`) plus the function's own three guards
    (`SocialContent.py:210-216`). No `funfunctions` check -- see the module
    docstring's note on this real v1 asymmetry."""

    def test_pools_when_every_v1_condition_holds(self) -> None:
        assert sticker_autoreply._should_pool_sticker(  # noqa: SLF001
            _cfg(_FakeConfig()),
            has_username=True,
            chat_title="Clean Group",
            emoji="\U0001f600",
            set_name="CleanPack1",
        )

    def test_sfw_off_refuses(self) -> None:
        assert not sticker_autoreply._should_pool_sticker(  # noqa: SLF001
            _cfg(_FakeConfig(sfw=False)),
            has_username=True,
            chat_title="Clean Group",
            emoji="\U0001f600",
            set_name="CleanPack1",
        )

    def test_no_username_refuses(self) -> None:
        assert not sticker_autoreply._should_pool_sticker(  # noqa: SLF001
            _cfg(_FakeConfig()),
            has_username=False,
            chat_title="Clean Group",
            emoji="\U0001f600",
            set_name="CleanPack1",
        )

    def test_fun_functions_off_still_pools(self) -> None:
        """The asymmetry itself: pooling has no funfunctions gate in v1."""
        assert sticker_autoreply._should_pool_sticker(  # noqa: SLF001
            _cfg(_FakeConfig(functions_fun=False)),
            has_username=True,
            chat_title="Clean Group",
            emoji="\U0001f600",
            set_name="CleanPack1",
        )

    def test_nsfw_title_refuses(self) -> None:
        assert not sticker_autoreply._should_pool_sticker(  # noqa: SLF001
            _cfg(_FakeConfig()),
            has_username=True,
            chat_title="NSFW Group",
            emoji="\U0001f600",
            set_name="CleanPack1",
        )

    def test_missing_emoji_refuses(self) -> None:
        assert not sticker_autoreply._should_pool_sticker(  # noqa: SLF001
            _cfg(_FakeConfig()),
            has_username=True,
            chat_title="Clean Group",
            emoji=None,
            set_name="CleanPack1",
        )

    def test_banned_emoji_refuses(self) -> None:
        assert not sticker_autoreply._should_pool_sticker(  # noqa: SLF001
            _cfg(_FakeConfig()),
            has_username=True,
            chat_title="Clean Group",
            emoji="\U0001f346",
            set_name="CleanPack1",
        )

    def test_missing_set_name_refuses(self) -> None:
        assert not sticker_autoreply._should_pool_sticker(  # noqa: SLF001
            _cfg(_FakeConfig()),
            has_username=True,
            chat_title="Clean Group",
            emoji="\U0001f600",
            set_name=None,
        )

    def test_non_alphanumeric_set_name_refuses(self) -> None:
        assert not sticker_autoreply._should_pool_sticker(  # noqa: SLF001
            _cfg(_FakeConfig()),
            has_username=True,
            chat_title="Clean Group",
            emoji="\U0001f600",
            set_name="not valid!",
        )


# --------------------------------------------------------------------- sticker_update


class TestStickerUpdate:
    """The combined sticker-branch handler: pooling + reply, always
    `SkipHandler` (module docstring's router-ordering note)."""

    async def test_pools_and_yields_for_a_qualifying_sticker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _FakeCtx(config=_FakeConfig(functions_fun=False))  # fun off: no reply expected
        monkeypatch.setattr(sticker_autoreply, "context_for", AsyncMock(return_value=ctx))
        pool = AsyncMock()
        monkeypatch.setattr(sticker_autoreply, "_pool_sticker", pool)
        reply = AsyncMock()
        monkeypatch.setattr(sticker_autoreply, "_reply_with_sticker", reply)

        message = _FakeMessage(sticker=_FakeSticker(file_id="pack-sticker-1"))
        with pytest.raises(SkipHandler):
            await sticker_autoreply.sticker_update(_msg(message), bot=cast(Any, _FakeBot()))

        pool.assert_awaited_once_with("pack-sticker-1")
        reply.assert_not_awaited()

    async def test_does_not_pool_when_should_pool_is_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _FakeCtx(config=_FakeConfig(sfw=False))
        monkeypatch.setattr(sticker_autoreply, "context_for", AsyncMock(return_value=ctx))
        pool = AsyncMock()
        monkeypatch.setattr(sticker_autoreply, "_pool_sticker", pool)
        monkeypatch.setattr(sticker_autoreply, "_reply_with_sticker", AsyncMock())

        message = _FakeMessage(sticker=_FakeSticker())
        with pytest.raises(SkipHandler):
            await sticker_autoreply.sticker_update(_msg(message), bot=cast(Any, _FakeBot()))

        pool.assert_not_awaited()

    async def test_replies_when_fun_on_and_reply_is_to_the_bot(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _FakeCtx(config=_FakeConfig(sfw=False, functions_fun=True))  # sfw off: no pooling
        monkeypatch.setattr(sticker_autoreply, "context_for", AsyncMock(return_value=ctx))
        pool = AsyncMock()
        monkeypatch.setattr(sticker_autoreply, "_pool_sticker", pool)
        reply = AsyncMock()
        monkeypatch.setattr(sticker_autoreply, "_reply_with_sticker", reply)

        bot_reply = _FakeMessage(from_user=_FakeUser(id=424242))
        message = _FakeMessage(sticker=_FakeSticker(), reply_to_message=bot_reply)
        with pytest.raises(SkipHandler):
            await sticker_autoreply.sticker_update(_msg(message), bot=cast(Any, _FakeBot()))

        pool.assert_not_awaited()
        reply.assert_awaited_once_with(_msg(message))

    async def test_a_failure_never_escapes_skiphandler_still_raised(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            sticker_autoreply, "context_for", AsyncMock(side_effect=RuntimeError("db down"))
        )

        message = _FakeMessage(sticker=_FakeSticker())
        with pytest.raises(SkipHandler):
            await sticker_autoreply.sticker_update(_msg(message), bot=cast(Any, _FakeBot()))


# --------------------------------------------------- reply_to_document_or_animation


class TestReplyToDocumentOrAnimation:
    async def test_not_a_reply_to_the_bot_never_calls_context_for(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        context_for = AsyncMock()
        monkeypatch.setattr(sticker_autoreply, "context_for", context_for)
        reply = AsyncMock()
        monkeypatch.setattr(sticker_autoreply, "_reply_with_sticker", reply)

        message = _FakeMessage(document=object())
        with pytest.raises(SkipHandler):
            await sticker_autoreply.reply_to_document_or_animation(
                _msg(message), bot=cast(Any, _FakeBot())
            )

        context_for.assert_not_awaited()
        reply.assert_not_awaited()

    async def test_fun_off_does_not_reply(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx = _FakeCtx(config=_FakeConfig(functions_fun=False))
        monkeypatch.setattr(sticker_autoreply, "context_for", AsyncMock(return_value=ctx))
        reply = AsyncMock()
        monkeypatch.setattr(sticker_autoreply, "_reply_with_sticker", reply)

        bot_reply = _FakeMessage(from_user=_FakeUser(id=424242))
        message = _FakeMessage(animation=object(), reply_to_message=bot_reply)
        with pytest.raises(SkipHandler):
            await sticker_autoreply.reply_to_document_or_animation(
                _msg(message), bot=cast(Any, _FakeBot())
            )

        reply.assert_not_awaited()

    async def test_fun_on_and_reply_to_bot_sends_a_sticker_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _FakeCtx(config=_FakeConfig(functions_fun=True))
        monkeypatch.setattr(sticker_autoreply, "context_for", AsyncMock(return_value=ctx))
        reply = AsyncMock()
        monkeypatch.setattr(sticker_autoreply, "_reply_with_sticker", reply)

        bot_reply = _FakeMessage(from_user=_FakeUser(id=424242))
        message = _FakeMessage(document=object(), reply_to_message=bot_reply)
        with pytest.raises(SkipHandler):
            await sticker_autoreply.reply_to_document_or_animation(
                _msg(message), bot=cast(Any, _FakeBot())
            )

        reply.assert_awaited_once_with(_msg(message))


# -------------------------------------------------------------------- _reply_with_sticker


class TestReplyWithSticker:
    async def test_empty_pool_sends_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sticker_autoreply, "_random_sticker", AsyncMock(return_value=None))
        message = _FakeMessage()

        await sticker_autoreply._reply_with_sticker(_msg(message))  # noqa: SLF001

        message.reply_sticker.assert_not_awaited()

    async def test_a_pooled_sticker_is_sent_as_a_reply(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            sticker_autoreply, "_random_sticker", AsyncMock(return_value="pooled-file-id")
        )
        message = _FakeMessage()

        await sticker_autoreply._reply_with_sticker(_msg(message))  # noqa: SLF001

        message.reply_sticker.assert_awaited_once_with("pooled-file-id")
