"""The pixels and the samples — x_distortion's actual work, with no Telegram in it.

v1: `../COOKIEBOT-Telegram-Group-Bot/Bot/Distortioner.py`. Pure functions here so
`packages/cb-worker/tests/test_distort.py` can run them on real bytes without a
bot, a queue or a network.

## What v1 does, and what of it is reachable

`distortioner()` (`:110-165`) branches on the file extension into three arms:
video, image and audio. **The video arm is unreachable from the bot**: every
call site that would hit it answers `destroy.video` or `destroy.gif` first
(`Miscellaneous.py:395-397,428-430`) — v1 disabled video and GIF distortion in
the handler and left the pipeline behind it in the tree. So the whole
`cv2`/frame-queue/`TicketedDict` machinery (`:14-98`), the ten frame workers and
the ffmpeg re-encodes are not ported: nothing can reach them. `destroy.video`
and `destroy.gif` are still answered, verbatim, from the handler.

That leaves two arms:

* **images** — `process_image` (`:37-44`): `liquid_rescale` to `distort`% of each
  dimension, then `resize` back to the original. Called with `distort=25` for a
  photo, a profile photo and a static sticker (`Miscellaneous.py:381,404,423`).
* **audio** — `distort_audiofile` (`:106-108`): ffmpeg's `vibrato` filter at
  `f=10, d=1`, the same two constants at every call site.

## Seam carving without ImageMagick

`liquid_rescale` is ImageMagick's content-aware resize (seam carving), reached
in v1 through `wand`. Reproducing it that way would mean a `liblqr`-enabled
ImageMagick in the runtime image — a system dependency the wolfi-based worker
image does not carry, on top of a Python binding for it. `seam_carve` below is
the same algorithm (Avidan & Shamir: repeatedly remove the minimum-energy
connected seam) over numpy, which is C-backed, already the natural companion to
the Pillow this package uses for `util_birthday`'s collage, and needs nothing
outside the wheel.

The carve runs on a **bounded** copy (`MAX_CARVE_DIMENSION`), not the original.
v1 carved whatever Telegram sent — up to 1280px for a photo — and then threw
the resolution away again by resizing back to the original size. Bounding the
intermediate keeps a single `/destroy` inside a second of CPU instead of tens
of seconds, and the visible result is the same distortion at the same output
size, because the last step is an upscale either way.
"""

from __future__ import annotations

import asyncio
import io
import subprocess
import tempfile
from pathlib import Path

import numpy as np
from numpy.typing import NDArray
from PIL import Image

from cb_core.logging import get_logger

log = get_logger("cb.worker.distort")

#: v1's one image ratio, at all three of its call sites (`Miscellaneous.py:381,404,423`).
DEFAULT_DISTORT_PERCENT = 25

#: v1's vibrato constants, likewise identical at every call site
#: (`Miscellaneous.py:412`, `Distortioner.py:159`).
AUDIO_FREQUENCY = 10.0
AUDIO_MODULATION = 1.0

#: The longest side the seam carve itself runs at. See the module docstring —
#: v1 carved at full resolution and then discarded it.
MAX_CARVE_DIMENSION = 256

#: How long ffmpeg gets before the job gives up on it. v1 called
#: `ffmpeg.run()` with no timeout at all, so a malformed file could wedge a
#: worker thread for as long as ffmpeg cared to sit there.
FFMPEG_TIMEOUT_SECONDS = 60.0


# --------------------------------------------------------------------- images


def _energy(gray: NDArray[np.float64]) -> NDArray[np.float64]:
    """Gradient magnitude — the standard seam-carving energy function."""
    dy, dx = np.gradient(gray)
    return np.abs(dx) + np.abs(dy)


def _seam_column_indices(energy: NDArray[np.float64]) -> NDArray[np.intp]:
    """The x of the minimum-energy top-to-bottom connected seam, per row.

    Forward pass accumulates the cheapest path cost into each pixel; the
    backtrack walks it up from the cheapest bottom pixel. Both loops are over
    *rows*, with the width vectorised, which is what keeps this cheap enough to
    run hundreds of times.
    """
    height, width = energy.shape
    cost = energy.copy()
    back = np.zeros((height, width), dtype=np.intp)
    for y in range(1, height):
        previous = cost[y - 1]
        left = np.concatenate(([np.inf], previous[:-1]))
        right = np.concatenate((previous[1:], [np.inf]))
        stacked = np.vstack((left, previous, right))
        choice = np.argmin(stacked, axis=0)
        back[y] = np.arange(width) + (choice - 1)
        cost[y] += stacked[choice, np.arange(width)]

    seam = np.zeros(height, dtype=np.intp)
    seam[height - 1] = int(np.argmin(cost[height - 1]))
    for y in range(height - 2, -1, -1):
        seam[y] = back[y + 1, seam[y + 1]]
    return seam


def _remove_seam(pixels: NDArray[np.uint8], seam: NDArray[np.intp]) -> NDArray[np.uint8]:
    """Drop one pixel per row, shrinking the image by exactly one column."""
    height, width, channels = pixels.shape
    mask = np.ones((height, width), dtype=bool)
    mask[np.arange(height), seam] = False
    return pixels[mask].reshape(height, width - 1, channels)


def _carve_columns(pixels: NDArray[np.uint8], target_width: int) -> NDArray[np.uint8]:
    while pixels.shape[1] > target_width:
        gray = pixels.astype(np.float64).mean(axis=2)
        pixels = _remove_seam(pixels, _seam_column_indices(_energy(gray)))
    return pixels


def seam_carve(image: Image.Image, percent: int) -> Image.Image:
    """`wand`'s `liquid_rescale(w*p, h*p)` — content-aware, both axes.

    Rows are carved by transposing and reusing the column pass, which is what
    ImageMagick does internally too. A `percent` that would leave a zero-width
    or zero-height image is clamped to one pixel, so a 1x1 avatar cannot make
    this raise where v1 would have.
    """
    pixels = np.asarray(image.convert("RGB"), dtype=np.uint8)
    height, width = pixels.shape[0], pixels.shape[1]
    target_width = max(1, int(width * (percent / 100.0)))
    target_height = max(1, int(height * (percent / 100.0)))

    pixels = _carve_columns(pixels, target_width)
    pixels = _carve_columns(np.transpose(pixels, (1, 0, 2)), target_height)
    return Image.fromarray(np.transpose(pixels, (1, 0, 2)))


def distort_image(
    data: bytes, *, percent: int = DEFAULT_DISTORT_PERCENT, fmt: str = "PNG"
) -> bytes:
    """v1's `process_image(source, destination, distort)` (`:37-44`).

    Carve to `percent` of each dimension, then resize back to the original —
    so the output is the same size as the input and the content is squeezed.
    `fmt` is `"PNG"` for stickers (v1 writes `distorted.png`, `:423`) and
    `"JPEG"` for photos (`distorted.jpg`, `:381,404`).
    """
    with Image.open(io.BytesIO(data)) as opened:
        original = opened.convert("RGB")
        original.load()

    working = original.copy()
    working.thumbnail((MAX_CARVE_DIMENSION, MAX_CARVE_DIMENSION), Image.Resampling.LANCZOS)

    distorted = seam_carve(working, percent)
    distorted = distorted.resize(original.size, Image.Resampling.LANCZOS)

    buffer = io.BytesIO()
    distorted.save(buffer, format=fmt)
    return buffer.getvalue()


async def distort_image_async(
    data: bytes, *, percent: int = DEFAULT_DISTORT_PERCENT, fmt: str = "PNG"
) -> bytes:
    """`distort_image` off the event loop.

    The carve is a few hundred milliseconds of pure CPU with the GIL held for
    numpy's C loops only; running it inline would stall every other job this
    worker is running. v1's answer to the same problem was three module-global
    booleans spun on with `while SEMAPHORE_IMAGES: pass` (`:114,145,155`) —
    FEATURE-MAP D3, a busy-wait that burned a whole core while it waited. The
    real bound lives in `cb_worker/jobs/distortion.py`'s semaphore.
    """
    return await asyncio.to_thread(distort_image, data, percent=percent, fmt=fmt)


# ---------------------------------------------------------------------- audio


def distort_audio(source: Path, destination: Path) -> None:
    """v1's `distort_audiofile` (`:106-108`) — ffmpeg's `vibrato` filter.

    v1 built the same command through `ffmpeg-python`, a wrapper whose only job
    here is to assemble this argument list; `subprocess` is what
    `cb_worker/bucket_export` and the rest of this codebase already reach for,
    and it is the one place a timeout can actually be set.
    """
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-af",
            f"vibrato=f={AUDIO_FREQUENCY}:d={AUDIO_MODULATION}",
            str(destination),
        ],
        check=True,
        capture_output=True,
        timeout=FFMPEG_TIMEOUT_SECONDS,
    )


async def distort_audio_bytes(data: bytes, *, suffix: str = ".ogg") -> bytes:
    """The audio arm end to end, on a private temporary directory.

    v1 wrote `distorted.mp3` into the process's working directory and deleted
    it afterwards (`Miscellaneous.py:412-415`) — a fixed, non-namespaced name
    shared by every concurrent request, which is FEATURE-MAP D4. A per-call
    `TemporaryDirectory` cannot collide with anything.
    """

    def _run() -> bytes:
        with tempfile.TemporaryDirectory(prefix="cb-distort-") as tmp:
            root = Path(tmp)
            source = root / f"input{suffix}"
            destination = root / "distorted.mp3"
            source.write_bytes(data)
            distort_audio(source, destination)
            return destination.read_bytes()

    return await asyncio.to_thread(_run)


__all__ = [
    "AUDIO_FREQUENCY",
    "AUDIO_MODULATION",
    "DEFAULT_DISTORT_PERCENT",
    "FFMPEG_TIMEOUT_SECONDS",
    "MAX_CARVE_DIMENSION",
    "distort_audio",
    "distort_audio_bytes",
    "distort_image",
    "distort_image_async",
    "seam_carve",
]
