"""Real file bytes, so image features can actually be validated.

The sandbox used to answer `getFile` with a fixed placeholder blob and render
every photo in the web client as a grey box with an emoji in it. That is
enough to prove a handler *ran*, and nothing at all about whether it did the
right thing: a bot that resizes an image, reads its dimensions, sniffs its
mime type, stores it, or refuses it for being too large has no observable
behaviour here without real bytes on both sides of the round trip.

So there is a file store. It is deliberately small:

* Content-addressed. A `file_id` is derived from the SHA-256 of the bytes, so
  uploading the same picture twice is the same file — which also matches the
  one real Telegram behaviour that bites people, where a re-sent file keeps
  its `file_unique_id`.
* In memory, mirrored to DuckDB like everything else in `state.py`, so a run
  can be reopened afterwards and still show its pictures.
* Bounded. A workbench that quietly grows a gigabyte database because someone
  dragged a video into it is a workbench people stop trusting.

Dimensions and mime type are sniffed from the bytes rather than trusted from
the uploader: `photo.width`/`height` are fields a handler can branch on, and a
sandbox that echoed back whatever the browser guessed would validate the
browser instead of the bot.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import struct
from dataclasses import dataclass

from cb_sandbox.logging import get_logger

log = get_logger("cb.sandbox.files")

#: Refuse anything larger. Real Telegram's own limits are far higher (50 MB for
#: a document), but this store lives in the same process and the same DuckDB
#: file as the rest of the world, and nothing about validating an image feature
#: needs a 50 MB image.
MAX_FILE_BYTES = 8 * 1024 * 1024

#: Total across all files in one run. A tester attaching the same handful of
#: pictures repeatedly is fine (content-addressing collapses those); a script
#: uploading thousands of distinct ones should hit a wall rather than swap.
MAX_STORE_BYTES = 128 * 1024 * 1024


@dataclass(slots=True)
class SandboxFile:
    """One stored blob, plus everything the Bot API shapes need to describe it."""

    file_id: str
    file_unique_id: str
    data: bytes
    mime_type: str
    file_name: str
    width: int = 0
    height: int = 0
    #: Set for the kinds that carry one on the wire (`video`, `animation`,
    #: `audio`, `voice`). Zero is what real Telegram sends when it doesn't know.
    duration: int = 0

    @property
    def size(self) -> int:
        return len(self.data)


# ------------------------------------------------------------------ sniffing


def _png_size(data: bytes) -> tuple[int, int] | None:
    # IHDR is always the first chunk, at a fixed offset — no parser needed.
    if len(data) < 24 or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    width, height = struct.unpack(">II", data[16:24])
    return int(width), int(height)


def _gif_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 10 or not data.startswith((b"GIF87a", b"GIF89a")):
        return None
    width, height = struct.unpack("<HH", data[6:10])
    return int(width), int(height)


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    """Walk the JPEG marker segments to the first frame header.

    Unlike PNG and GIF, JPEG has no fixed-offset size field: the dimensions
    live in whichever SOF marker the encoder used, after an arbitrary number
    of application and quantisation segments. Short, but it genuinely has to
    be a loop.
    """
    if not data.startswith(b"\xff\xd8"):
        return None
    index = 2
    length = len(data)
    while index + 9 < length:
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        # Standalone markers (padding, restart) carry no length field.
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        segment_length = int.from_bytes(data[index + 2 : index + 4], "big")
        # SOF0..SOF15, excluding the DHT/JPG/DAC markers interleaved in that
        # range — every one of them lays out height then width at the same
        # offset within the segment.
        if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
            height = int.from_bytes(data[index + 5 : index + 7], "big")
            width = int.from_bytes(data[index + 7 : index + 9], "big")
            return width, height
        if segment_length <= 0:
            return None
        index += 2 + segment_length
    return None


def _webp_size(data: bytes) -> tuple[int, int] | None:
    """WebP — the format Telegram stickers actually are, so worth handling
    rather than letting every sticker render at 0x0."""
    if len(data) < 30 or not data.startswith(b"RIFF") or data[8:12] != b"WEBP":
        return None
    chunk = data[12:16]
    if chunk == b"VP8X":
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return width, height
    if chunk == b"VP8L":
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if chunk == b"VP8 ":
        return (
            int.from_bytes(data[26:28], "little") & 0x3FFF,
            int.from_bytes(data[28:30], "little") & 0x3FFF,
        )
    return None


#: Magic-number prefixes, longest-first so `RIFF....WEBP` never loses to a
#: shorter prefix that happens to overlap.
_MIME_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"%PDF-", "application/pdf"),
    (b"OggS", "audio/ogg"),
    (b"ID3", "audio/mpeg"),
    (b"\x1a\x45\xdf\xa3", "video/webm"),
)


def sniff(data: bytes, *, fallback: str = "application/octet-stream") -> str:
    """The real content type, read from the bytes.

    Deliberately not the browser's `File.type` or the uploader's claim: a
    handler that content-sniffs (or that trusts Telegram's own `mime_type`)
    is exactly the code this sandbox is meant to test, and feeding it a
    declared type would test the declaration instead.
    """
    for signature, mime in _MIME_SIGNATURES:
        if data.startswith(signature):
            return mime
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:12] in (b"ftypisom", b"ftypmp42") or data[4:8] == b"ftyp":
        return "video/mp4"
    return fallback


def dimensions(data: bytes) -> tuple[int, int]:
    """`(width, height)`, or `(0, 0)` for anything that isn't a still image —
    which is what real Telegram sends when it doesn't know either."""
    for reader in (_png_size, _gif_size, _jpeg_size, _webp_size):
        size = reader(data)
        if size is not None:
            return size
    return 0, 0


# --------------------------------------------------------------------- store


class FileStore:
    """Every blob this run has seen, keyed by content.

    Owned by `SandboxStore`; `control_api` writes to it when a tester attaches
    something, `telegram_api` writes to it when the *bot* uploads something,
    and both read from it to answer `getFile` and the file download route with
    real bytes.
    """

    def __init__(self) -> None:
        self._files: dict[str, SandboxFile] = {}
        self._total_bytes = 0

    def __contains__(self, file_id: object) -> bool:
        return file_id in self._files

    def __len__(self) -> int:
        return len(self._files)

    @property
    def total_bytes(self) -> int:
        return self._total_bytes

    def get(self, file_id: str) -> SandboxFile | None:
        return self._files.get(file_id)

    def add(
        self,
        data: bytes,
        *,
        file_name: str = "",
        declared_mime: str | None = None,
        duration: int = 0,
    ) -> SandboxFile:
        """Store bytes and return the file. Idempotent by content.

        `declared_mime` is only a fallback for bytes whose magic number this
        module doesn't know — a sniffed type always wins, because that is what
        a real handler would see.
        """
        if not data:
            raise ValueError("refusing to store an empty file")
        if len(data) > MAX_FILE_BYTES:
            raise ValueError(
                f"file is {len(data)} bytes, over the sandbox limit of {MAX_FILE_BYTES}"
            )

        digest = hashlib.sha256(data).hexdigest()
        file_id = f"sbx-{digest[:32]}"
        existing = self._files.get(file_id)
        if existing is not None:
            return existing

        if self._total_bytes + len(data) > MAX_STORE_BYTES:
            raise ValueError(
                f"sandbox file store is full ({self._total_bytes} bytes); reset to clear it"
            )

        mime = sniff(data, fallback=declared_mime or "application/octet-stream")
        width, height = dimensions(data)
        stored = SandboxFile(
            file_id=file_id,
            # Real Telegram's `file_unique_id` is stable for the same content
            # across chats and bots while `file_id` is not; content-addressing
            # gives that property for free, so both derive from the digest.
            file_unique_id=f"u-{digest[32:48]}",
            data=data,
            mime_type=mime,
            file_name=file_name or _default_name(mime),
            width=width,
            height=height,
            duration=duration,
        )
        self._files[file_id] = stored
        self._total_bytes += len(data)
        return stored

    def add_base64(self, encoded: str, **kwargs: object) -> SandboxFile:
        """What the web client posts: a browser reads a picked file with
        `FileReader` and sends the base64 payload of the resulting data URL."""
        payload = encoded.split(",", 1)[-1] if encoded.startswith("data:") else encoded
        try:
            data = base64.b64decode(payload, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(f"not valid base64: {exc}") from exc
        return self.add(data, **kwargs)  # type: ignore[arg-type]

    def clear(self) -> None:
        self._files.clear()
        self._total_bytes = 0

    def all(self) -> list[SandboxFile]:
        return list(self._files.values())

    def restore(self, stored: SandboxFile) -> None:
        """Put a file back without re-deriving its id — used by the persistence
        layer on load, where the id already on disk is the authority."""
        if stored.file_id in self._files:
            return
        self._files[stored.file_id] = stored
        self._total_bytes += stored.size


def _default_name(mime: str) -> str:
    extension = {
        "image/png": "png",
        "image/jpeg": "jpg",
        "image/gif": "gif",
        "image/webp": "webp",
        "video/mp4": "mp4",
        "video/webm": "webm",
        "audio/ogg": "ogg",
        "audio/mpeg": "mp3",
        "application/pdf": "pdf",
    }.get(mime, "bin")
    return f"sandbox-file.{extension}"


#: What a media message carries when nobody uploaded anything — a bot re-sending
#: a `file_id` it got from production, a seeded fixture, a test that only cares
#: that *a* photo was sent. Kept distinct from "no media at all" so the client
#: can say "a photo, contents unknown" instead of drawing a broken image.
PLACEHOLDER_FILE_IDS: dict[str, str] = {
    "photo": "sandbox-photo",
    "sticker": "sandbox-sticker",
    "video": "sandbox-video",
    "animation": "sandbox-animation",
    "document": "sandbox-document",
    "audio": "sandbox-audio",
    "voice": "sandbox-voice",
}
