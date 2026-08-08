"""`cb_worker.jobs.distortion` — the send branch, the failure branch and the
bound that replaces v1's spin lock.

The distortion itself is `test_distort.py`'s subject; here the CPU work is
stubbed so the job's own decisions are what is under test.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from cb_worker.jobs import distortion


class _FakeBuffer:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class _FakeBot:
    """Records what the job sent, the way MockTelegram does for the gateway."""

    def __init__(self, *, download: bytes | None = b"input-bytes") -> None:
        self._download = download
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def download(self, file_id: str) -> _FakeBuffer | None:
        if self._download is None:
            raise RuntimeError("file is gone")
        return _FakeBuffer(self._download)

    async def send_photo(self, chat_id: int, photo: Any, **kwargs: Any) -> None:
        self.calls.append(("send_photo", {"chat_id": chat_id, "file": photo, **kwargs}))

    async def send_sticker(self, chat_id: int, sticker: Any, **kwargs: Any) -> None:
        self.calls.append(("send_sticker", {"chat_id": chat_id, "file": sticker, **kwargs}))

    async def send_audio(self, chat_id: int, audio: Any, **kwargs: Any) -> None:
        self.calls.append(("send_audio", {"chat_id": chat_id, "file": audio, **kwargs}))

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> None:
        self.calls.append(("send_message", {"chat_id": chat_id, "text": text, **kwargs}))


@pytest.fixture(autouse=True)
def _reset_bound() -> Any:
    distortion.reset_concurrency_bound()
    yield
    distortion.reset_concurrency_bound()


@pytest.fixture
def stub_work(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _image(data: bytes, *, percent: int = 25, fmt: str = "PNG") -> bytes:
        return b"distorted-" + fmt.encode()

    async def _audio(data: bytes, *, suffix: str = ".ogg") -> bytes:
        return b"distorted-audio"

    monkeypatch.setattr(distortion, "distort_image_async", _image)
    monkeypatch.setattr(distortion, "distort_audio_bytes", _audio)


async def _run(bot: _FakeBot, kind: str) -> None:
    await distortion.distort_media(
        {"bot": bot},
        group_id=-100,
        message_id=7,
        file_id="file-1",
        kind=kind,
        lang="en",
    )


@pytest.mark.parametrize(
    ("kind", "method", "filename"),
    [
        ("photo", "send_photo", "distorted.jpg"),
        ("sticker", "send_sticker", "distorted.png"),
        ("audio", "send_audio", "distorted.mp3"),
    ],
)
async def test_each_kind_uses_v1s_own_send_call(
    stub_work: None, kind: str, method: str, filename: str
) -> None:
    """v1 sends a photo with `sendPhoto`, a sticker with `sendSticker` and both
    an audio file *and* a voice note with `sendAudio`
    (`Miscellaneous.py:389,413,425`), always as a reply."""
    bot = _FakeBot()
    await _run(bot, kind)
    assert [name for name, _ in bot.calls] == [method]
    payload = bot.calls[0][1]
    assert payload["reply_to_message_id"] == 7
    assert payload["file"].filename == filename


async def test_sticker_output_is_png_and_photo_output_is_jpeg(stub_work: None) -> None:
    """v1 writes `distorted.png` for a sticker and `distorted.jpg` for a photo,
    which is the only thing that differs between the two arms."""
    sticker_bot, photo_bot = _FakeBot(), _FakeBot()
    await _run(sticker_bot, "sticker")
    await _run(photo_bot, "photo")
    assert sticker_bot.calls[0][1]["file"].data == b"distorted-PNG"
    assert photo_bot.calls[0][1]["file"].data == b"distorted-JPEG"


async def test_a_download_failure_answers_the_instruction_string(stub_work: None) -> None:
    bot = _FakeBot(download=None)
    await _run(bot, "photo")
    assert [name for name, _ in bot.calls] == ["send_message"]
    assert "Reply to a photo" in bot.calls[0][1]["text"]


async def test_a_distortion_failure_answers_rather_than_going_silent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v1 printed the exception and sent nothing at all
    (`Distortioner.py:132-133`)."""

    async def _boom(data: bytes, *, percent: int = 25, fmt: str = "PNG") -> bytes:
        raise ValueError("cannot identify image file")

    monkeypatch.setattr(distortion, "distort_image_async", _boom)
    bot = _FakeBot()
    await _run(bot, "photo")
    assert [name for name, _ in bot.calls] == ["send_message"]


async def test_concurrency_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """The replacement for `while SEMAPHORE_IMAGES: pass` (FEATURE-MAP D3).
    Four jobs are started at once against a bound of two; at no point may more
    than two be inside the distortion step."""
    from cb_core.settings import Settings

    monkeypatch.setattr(distortion, "get_settings", lambda: Settings(distortion_concurrency=2))

    inside = 0
    peak = 0

    async def _slow(data: bytes, *, percent: int = 25, fmt: str = "PNG") -> bytes:
        nonlocal inside, peak
        inside += 1
        peak = max(peak, inside)
        await asyncio.sleep(0.02)
        inside -= 1
        return b"x"

    monkeypatch.setattr(distortion, "distort_image_async", _slow)
    bots = [_FakeBot() for _ in range(4)]
    await asyncio.gather(*(_run(bot, "photo") for bot in bots))
    assert peak == 2, f"the semaphore let {peak} jobs in at once"
    assert all(bot.calls[0][0] == "send_photo" for bot in bots)
