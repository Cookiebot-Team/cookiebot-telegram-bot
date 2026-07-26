"""Unit coverage for fun_random — pure logic and handler orchestration, no
dispatcher, no real Telegram, no DB.

See docs/contracts/fun_random.md for the full behaviour contract,
qa/features/fun_random.feature + qa/test_fun_random.py for the end-to-end
version of the read side, and qa/integration/test_fun_random.py for the real
`media_objects` round trip on both the write and the read side.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.types import Message

from cb_core.group_config import GroupConfig
from cb_core.storage import MediaRef
from cb_gateway.handlers import fun_random


@dataclass
class _FakeUser:
    id: int
    username: str | None = None
    first_name: str | None = None
    is_bot: bool = False


@dataclass
class _FakePhotoSize:
    file_id: str


@dataclass
class _FakeVideo:
    file_id: str
    mime_type: str | None = None


@dataclass
class _FakeChat:
    id: int = -100
    title: str | None = "QA Group"


@dataclass
class _FakeMessage:
    """Only the attributes the handler actually reads."""

    from_user: _FakeUser | None = None
    photo: Any = None
    video: Any = None
    forward_origin: Any = None
    chat: Any = field(default_factory=_FakeChat)
    bot: Any = None
    answer: Any = field(default_factory=AsyncMock)
    answer_photo: Any = field(default_factory=AsyncMock)
    answer_video: Any = field(default_factory=AsyncMock)


class _FakeConfig:
    def __init__(
        self,
        *,
        sfw: bool = True,
        functions_fun: bool = True,
        publisher_post: bool = False,
    ) -> None:
        self.sfw = sfw
        self.functions_fun = functions_fun
        self.publisher_post = publisher_post


class _FakeCtx:
    def __init__(self, *, group_id: int = -100, config: _FakeConfig | None = None) -> None:
        self.group_id = group_id
        self.lang = "en"
        self.config = config or _FakeConfig()
        self._fun_enabled = self.config.functions_fun

    def enabled(self, area: str) -> bool:
        assert area == "fun"
        return self._fun_enabled


def _ref(*, kind: str = "photo", telegram_file_id: str | None = "tg-file-1") -> MediaRef:
    from uuid import uuid4

    return MediaRef(
        media_id=uuid4(),
        group_id=-100,
        kind=kind,
        content_hash="deadbeef",
        blob_key=f"media/{kind}/de/deadbeef.jpg",
        byte_size=3,
        telegram_file_id=telegram_file_id,
    )


def _msg(message: _FakeMessage) -> Message:
    """The handler only ever reads a handful of attributes off `Message`
    (see `_FakeMessage`'s own docstring); this cast documents that the fake
    stands in for the real aiogram type at the seam, deliberately, rather than
    constructing a full pydantic `Message` per test."""
    return cast(Message, message)


def _cfg(config: _FakeConfig) -> GroupConfig:
    """Same idea as `_msg` above, for `GroupConfig`."""
    return cast(GroupConfig, config)


# --------------------------------------------------------------- _has_nsfw_title


class TestHasNsfwTitle:
    """`SocialContent.py:194`'s `BANNED_TITLESUBSTRINGS` check."""

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
        assert fun_random._has_nsfw_title(title)  # noqa: SLF001

    def test_a_clean_title_is_not_flagged(self) -> None:
        assert not fun_random._has_nsfw_title("Furry Art Chat")  # noqa: SLF001

    def test_none_title_is_not_flagged(self) -> None:
        assert not fun_random._has_nsfw_title(None)  # noqa: SLF001

    def test_empty_title_is_not_flagged(self) -> None:
        assert not fun_random._has_nsfw_title("")  # noqa: SLF001


# ------------------------------------------------------------------ _is_forwarded


class TestIsForwarded:
    def test_no_forward_origin_is_not_forwarded(self) -> None:
        message = _FakeMessage(forward_origin=None)
        assert not fun_random._is_forwarded(_msg(message))  # noqa: SLF001

    def test_a_forward_origin_marks_it_forwarded(self) -> None:
        message = _FakeMessage(forward_origin=object())
        assert fun_random._is_forwarded(_msg(message))  # noqa: SLF001


# ------------------------------------------------------------ _pool_kind_and_file_id


class TestPoolKindAndFileId:
    """`COOKIEBOT.py:168-172`: photo takes the *largest* size (`[-1]`)."""

    def test_photo_uses_the_largest_size(self) -> None:
        message = _FakeMessage(photo=[_FakePhotoSize("small"), _FakePhotoSize("large")])
        assert fun_random._pool_kind_and_file_id(_msg(message)) == ("photo", "large")  # noqa: SLF001

    def test_video_uses_the_video_object(self) -> None:
        message = _FakeMessage(video=_FakeVideo("video-1"))
        assert fun_random._pool_kind_and_file_id(_msg(message)) == ("video", "video-1")  # noqa: SLF001

    def test_neither_photo_nor_video_is_none(self) -> None:
        message = _FakeMessage()
        assert fun_random._pool_kind_and_file_id(_msg(message)) is None  # noqa: SLF001

    def test_empty_photo_list_is_none(self) -> None:
        message = _FakeMessage(photo=[])
        assert fun_random._pool_kind_and_file_id(_msg(message)) is None  # noqa: SLF001


# ----------------------------------------------------------------- _should_pool


class TestShouldPool:
    """`if sfw and funfunctions and not publisherpost:` (`COOKIEBOT.py:169,171`)
    plus `add_to_random_database`'s forwarded/title guards."""

    def test_pools_when_every_v1_condition_holds(self) -> None:
        assert fun_random._should_pool(  # noqa: SLF001
            _cfg(_FakeConfig()), chat_title="Clean Group", forwarded=False
        )

    def test_sfw_off_refuses(self) -> None:
        assert not fun_random._should_pool(  # noqa: SLF001
            _cfg(_FakeConfig(sfw=False)), chat_title="Clean Group", forwarded=False
        )

    def test_fun_functions_off_refuses(self) -> None:
        assert not fun_random._should_pool(  # noqa: SLF001
            _cfg(_FakeConfig(functions_fun=False)), chat_title="Clean Group", forwarded=False
        )

    def test_publisher_post_on_refuses(self) -> None:
        assert not fun_random._should_pool(  # noqa: SLF001
            _cfg(_FakeConfig(publisher_post=True)), chat_title="Clean Group", forwarded=False
        )

    def test_forwarded_message_refuses(self) -> None:
        assert not fun_random._should_pool(  # noqa: SLF001
            _cfg(_FakeConfig()), chat_title="Clean Group", forwarded=True
        )

    def test_nsfw_title_refuses(self) -> None:
        assert not fun_random._should_pool(  # noqa: SLF001
            _cfg(_FakeConfig()), chat_title="NSFW Group", forwarded=False
        )


# --------------------------------------------------------------- _content_type_for


class TestContentTypeFor:
    def test_photo_is_always_jpeg(self) -> None:
        assert (
            fun_random._content_type_for("photo", _msg(_FakeMessage())) == "image/jpeg"  # noqa: SLF001
        )

    def test_video_uses_its_own_mime_type(self) -> None:
        message = _FakeMessage(video=_FakeVideo("v1", mime_type="video/mp4"))
        assert fun_random._content_type_for("video", _msg(message)) == "video/mp4"  # noqa: SLF001

    def test_video_without_a_message_video_is_none(self) -> None:
        assert fun_random._content_type_for("video", _msg(_FakeMessage())) is None  # noqa: SLF001


# ------------------------------------------------------------------- pool_media


class TestPoolMedia:
    async def test_skips_when_neither_photo_nor_video(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        context_for = AsyncMock()
        monkeypatch.setattr(fun_random, "context_for", context_for)
        pool = AsyncMock()
        monkeypatch.setattr(fun_random, "_pool", pool)

        with pytest.raises(SkipHandler):
            await fun_random.pool_media(_FakeMessage(), bot=object())

        context_for.assert_not_awaited()
        pool.assert_not_awaited()

    async def test_does_not_pool_when_should_pool_is_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            fun_random,
            "context_for",
            AsyncMock(return_value=_FakeCtx(config=_FakeConfig(sfw=False))),
        )
        pool = AsyncMock()
        monkeypatch.setattr(fun_random, "_pool", pool)

        message = _FakeMessage(photo=[_FakePhotoSize("p1")], from_user=_FakeUser(id=1))
        with pytest.raises(SkipHandler):
            await fun_random.pool_media(message, bot=object())

        pool.assert_not_awaited()

    async def test_pools_a_qualifying_photo(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx = _FakeCtx(group_id=-777)
        monkeypatch.setattr(fun_random, "context_for", AsyncMock(return_value=ctx))
        pool = AsyncMock()
        monkeypatch.setattr(fun_random, "_pool", pool)

        bot = object()
        message = _FakeMessage(photo=[_FakePhotoSize("p1")], from_user=_FakeUser(id=42))
        with pytest.raises(SkipHandler):
            await fun_random.pool_media(message, bot=bot)

        pool.assert_awaited_once_with(bot, message, -777, "photo", "p1", uploaded_by=42)

    async def test_uploaded_by_is_none_without_a_sender(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(fun_random, "context_for", AsyncMock(return_value=_FakeCtx()))
        pool = AsyncMock()
        monkeypatch.setattr(fun_random, "_pool", pool)

        message = _FakeMessage(photo=[_FakePhotoSize("p1")], from_user=None)
        with pytest.raises(SkipHandler):
            await fun_random.pool_media(message, bot=object())

        assert pool.await_args is not None
        assert pool.await_args.kwargs["uploaded_by"] is None

    async def test_a_pooling_failure_still_raises_skip_handler(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reply path (any other router still interested in this photo) must
        never go down because storage hiccupped."""
        monkeypatch.setattr(fun_random, "context_for", AsyncMock(return_value=_FakeCtx()))

        async def boom(*args: object, **kwargs: object) -> None:
            raise RuntimeError("pg pool not initialised")

        monkeypatch.setattr(fun_random, "_pool", boom)

        message = _FakeMessage(photo=[_FakePhotoSize("p1")], from_user=_FakeUser(id=1))
        with pytest.raises(SkipHandler):
            await fun_random.pool_media(message, bot=object())


# --------------------------------------------------------------- send_random_media


class TestSendRandomMedia:
    async def test_fun_off_replies_with_fun_off_text_and_never_reads_the_pool(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            fun_random,
            "context_for",
            AsyncMock(return_value=_FakeCtx(config=_FakeConfig(functions_fun=False))),
        )
        select_media = AsyncMock()
        monkeypatch.setattr(fun_random, "_select_media", select_media)

        message = _FakeMessage()
        await fun_random.send_random_media(message, bot=object())

        message.answer.assert_awaited_once()
        select_media.assert_not_awaited()

    async def test_empty_pool_sends_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """v1 parity: `random_media` gives up silently after 50 failed attempts
        (`SocialContent.py:198-206`)."""
        monkeypatch.setattr(fun_random, "context_for", AsyncMock(return_value=_FakeCtx()))
        monkeypatch.setattr(fun_random, "_select_media", AsyncMock(return_value=None))

        message = _FakeMessage()
        await fun_random.send_random_media(message, bot=object())

        message.answer.assert_not_awaited()
        message.answer_photo.assert_not_awaited()
        message.answer_video.assert_not_awaited()

    async def test_a_found_photo_is_sent_via_send_media(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(fun_random, "context_for", AsyncMock(return_value=_FakeCtx()))
        ref = _ref(kind="photo")
        monkeypatch.setattr(fun_random, "_select_media", AsyncMock(return_value=ref))
        send_media = AsyncMock()
        monkeypatch.setattr(fun_random, "_send_media", send_media)

        message = _FakeMessage()
        await fun_random.send_random_media(message, bot=object())

        send_media.assert_awaited_once_with(message, ref)


# ------------------------------------------------------------------- _send_media


class TestSendMedia:
    async def test_photo_with_a_file_id_never_touches_the_blob_store(self) -> None:
        message = _FakeMessage()
        ref = _ref(kind="photo", telegram_file_id="tg-file-9")

        await fun_random._send_media(_msg(message), ref)  # noqa: SLF001

        message.answer_photo.assert_awaited_once_with("tg-file-9")
        message.answer_video.assert_not_awaited()

    async def test_video_with_a_file_id_uses_answer_video(self) -> None:
        message = _FakeMessage()
        ref = _ref(kind="video", telegram_file_id="tg-file-9")

        await fun_random._send_media(_msg(message), ref)  # noqa: SLF001

        message.answer_video.assert_awaited_once_with("tg-file-9")
        message.answer_photo.assert_not_awaited()

    async def test_missing_file_id_falls_back_to_stored_bytes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cb_core import storage

        class _FakeMediaService:
            async def get_bytes(self, ref: MediaRef) -> bytes:
                return b"stored bytes"

        monkeypatch.setattr(storage, "media", lambda: _FakeMediaService())

        message = _FakeMessage()
        ref = _ref(kind="photo", telegram_file_id=None)

        await fun_random._send_media(_msg(message), ref)  # noqa: SLF001

        message.answer_photo.assert_awaited_once()
        assert message.answer_photo.await_args is not None
        (sent_file,) = message.answer_photo.await_args.args
        assert sent_file.data == b"stored bytes"
