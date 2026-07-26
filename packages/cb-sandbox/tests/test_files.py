"""Real bytes, both directions.

An image feature has nothing to act on unless the sandbox carries the actual
file: a handler that reads dimensions, sniffs a mime type, resizes, stores or
rejects an upload was previously being fed the same 27-byte placeholder every
time, so every one of those branches passed or failed for reasons that had
nothing to do with the image.

The assertions worth reading here are the ones about *sniffing* — the sandbox
deliberately ignores the uploader's claim about what a file is, because what a
handler sees is what the bytes say.
"""

from __future__ import annotations

import base64
import struct
import zlib
from typing import Any

import pytest
from cb_sandbox.control_api import router as control_router
from cb_sandbox.files import FileStore, dimensions, sniff
from cb_sandbox.state import store
from cb_sandbox.telegram_api import router as telegram_router
from fastapi import FastAPI
from fastapi.testclient import TestClient


def png_bytes(width: int, height: int) -> bytes:
    """A real, decodable PNG — not a header stub. The dimension readers walk
    actual chunk structure, and a stub would let a broken reader pass."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


GIF_BYTES = b"GIF89a" + struct.pack("<HH", 12, 34) + b"\x00" * 10


@pytest.fixture
def client() -> TestClient:
    store().reset()
    app = FastAPI()
    app.include_router(control_router)
    app.include_router(telegram_router)
    return TestClient(app)


def _upload(client: TestClient, data: bytes, **body: Any) -> dict[str, Any]:
    resp = client.post("/api/files", json={"data": base64.b64encode(data).decode(), **body})
    assert resp.status_code == 201, resp.text
    return resp.json()


class TestSniffing:
    def test_dimensions_come_from_the_bytes(self) -> None:
        assert dimensions(png_bytes(120, 80)) == (120, 80)
        assert dimensions(GIF_BYTES) == (12, 34)

    def test_something_that_is_not_an_image_has_no_dimensions(self) -> None:
        assert dimensions(b"plain text, not a picture") == (0, 0)

    def test_the_declared_type_never_beats_the_real_one(self, client: TestClient) -> None:
        """A handler that content-sniffs a download is exactly the code this
        sandbox exists to test. Echoing back the uploader's claim would test
        the claim instead."""
        stored = _upload(client, png_bytes(2, 2), content_type="image/jpeg", filename="lies.jpg")
        assert stored["mime_type"] == "image/png"

    def test_an_unrecognised_format_falls_back_to_the_declared_type(self) -> None:
        assert sniff(b"\x00\x01\x02", fallback="application/x-thing") == "application/x-thing"


class TestStore:
    def test_the_same_bytes_are_the_same_file(self, client: TestClient) -> None:
        """Content addressing, which also matches the one real Telegram
        behaviour that bites people: a re-sent file keeps its
        `file_unique_id`."""
        first = _upload(client, png_bytes(4, 4))
        second = _upload(client, png_bytes(4, 4), filename="different-name.png")
        assert first["file_id"] == second["file_id"]
        assert first["file_unique_id"] == second["file_unique_id"]

    def test_an_oversized_file_is_refused_with_a_4xx(self, client: TestClient) -> None:
        store_limit = FileStore()
        with pytest.raises(ValueError, match="over the sandbox limit"):
            store_limit.add(b"x" * (8 * 1024 * 1024 + 1))

    def test_malformed_base64_is_a_400_not_a_500(self, client: TestClient) -> None:
        resp = client.post("/api/files", json={"data": "not base64 at all!!"})
        assert resp.status_code == 400

    def test_a_data_url_is_accepted_whole(self, client: TestClient) -> None:
        """What `FileReader.readAsDataURL` produces — accepting it as-is is
        one fewer string operation the browser can get wrong."""
        encoded = base64.b64encode(png_bytes(3, 3)).decode()
        resp = client.post("/api/files", json={"data": f"data:image/png;base64,{encoded}"})
        assert resp.status_code == 201
        assert resp.json()["width"] == 3

    def test_reset_clears_the_store(self, client: TestClient) -> None:
        _upload(client, png_bytes(2, 2))
        assert len(store().files) == 1
        client.post("/api/reset")
        assert len(store().files) == 0


class TestRoundTrip:
    def test_a_tester_attaching_a_photo_reaches_the_bot_with_real_dimensions(
        self, client: TestClient
    ) -> None:
        """The whole point. A handler that branches on `photo[-1].width` was
        previously seeing 640 for every image ever sent."""
        snapshot = client.post("/api/seed", json={"scenario": "default"}).json()
        chat_id = snapshot["chats"][0]["id"]
        bob = next(u for u in snapshot["users"] if u["username"] == "bob")
        stored = _upload(client, png_bytes(120, 80), filename="shot.png")

        resp = client.post(
            f"/api/chats/{chat_id}/messages",
            json={"user_id": bob["id"], "media": "photo", "media_file_id": stored["file_id"]},
        )
        assert resp.status_code == 201
        assert resp.json()["media_file_id"] == stored["file_id"]

        photo = store().pending_updates[-1]["message"]["photo"][-1]
        assert (photo["width"], photo["height"]) == (120, 80)
        assert photo["file_id"] == stored["file_id"]

    def test_the_bot_can_download_what_it_was_sent(self, client: TestClient) -> None:
        data = png_bytes(6, 6)
        stored = _upload(client, data)

        described = client.post("/bot424242:X/getFile", json={"file_id": stored["file_id"]}).json()[
            "result"
        ]
        assert described["file_size"] == len(data)

        downloaded = client.get(f"/file/bot424242:X/{described['file_path']}")
        assert downloaded.status_code == 200
        assert downloaded.content == data
        assert downloaded.headers["content-type"] == "image/png"

    def test_an_unknown_file_id_still_downloads_rather_than_404ing(
        self, client: TestClient
    ) -> None:
        """A bot re-sending a `file_id` minted by production is behaving
        correctly. Failing its download would break the handler under test for
        a reason that is about the sandbox, not about the handler."""
        described = client.post("/bot424242:X/getFile", json={"file_id": "from-prod"}).json()
        assert described["ok"] is True
        assert (
            client.get(f"/file/bot424242:X/{described['result']['file_path']}").status_code == 200
        )

    def test_a_photo_the_bot_uploads_is_kept_and_shown(self, client: TestClient) -> None:
        """The case most worth looking at: a bot that generates an image — a
        captcha, a chart, a resized thumbnail — is only validatable by seeing
        the picture it produced."""
        snapshot = client.post("/api/seed", json={"scenario": "default"}).json()
        chat_id = snapshot["chats"][0]["id"]
        data = png_bytes(64, 48)

        resp = client.post(
            "/bot424242:X/sendPhoto",
            data={"chat_id": str(chat_id)},
            files={"photo": ("generated.png", data, "image/png")},
        )
        assert resp.status_code == 200
        photo = resp.json()["result"]["photo"][-1]
        assert (photo["width"], photo["height"]) == (64, 48)

        message = store().messages[chat_id][-1]
        assert message.media_file_id == photo["file_id"]
        assert store().files.get(photo["file_id"]).data == data  # type: ignore[union-attr]

    def test_uploaded_bytes_never_reach_the_api_call_log(self, client: TestClient) -> None:
        """The log is read by a human and serialised to JSON. A megabyte of
        binary in it helps nobody and makes the panel unreadable."""
        snapshot = client.post("/api/seed", json={"scenario": "default"}).json()
        chat_id = snapshot["chats"][0]["id"]
        client.post(
            "/bot424242:X/sendPhoto",
            data={"chat_id": str(chat_id)},
            files={"photo": ("generated.png", png_bytes(8, 8), "image/png")},
        )
        payload = store().api_calls[-1]["payload"]
        assert "__uploads__" not in payload
        # The filename survives, under the parameter's own name — enough to
        # tell a reader what was sent without carrying the bytes.
        assert payload["photo"] == "generated.png"

    def test_media_with_no_file_still_renders_a_well_formed_payload(
        self, client: TestClient
    ) -> None:
        """A flood test sends six stickers and cares about none of their
        contents. The bot must still receive a valid `sticker` object — a
        missing `file_id` is a payload its own model would reject outright."""
        snapshot = client.post("/api/seed", json={"scenario": "default"}).json()
        chat_id = snapshot["chats"][0]["id"]
        bob = next(u for u in snapshot["users"] if u["username"] == "bob")
        client.post(
            f"/api/chats/{chat_id}/messages", json={"user_id": bob["id"], "media": "sticker"}
        )
        sticker = store().pending_updates[-1]["message"]["sticker"]
        assert sticker["file_id"]
        assert sticker["file_unique_id"]


def test_files_survive_a_restart(tmp_path: Any) -> None:
    """A run whose pictures did not survive is a run whose image features
    cannot be reviewed afterwards — which is most of the reason the sandbox
    keeps a database at all."""
    from cb_sandbox.state import SandboxStore

    db_path = str(tmp_path / "files.duckdb")
    data = png_bytes(10, 20)

    first = SandboxStore(db_path)
    stored = first.store_file(data, file_name="keep.png")
    first.close()

    second = SandboxStore(db_path)
    try:
        restored = second.files.get(stored.file_id)
        assert restored is not None
        assert restored.data == data
        assert (restored.width, restored.height) == (10, 20)
        assert restored.file_name == "keep.png"
    finally:
        second.close()
