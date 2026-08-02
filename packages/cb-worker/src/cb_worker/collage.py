"""Pure image compositing for `util_birthday`'s montage — no I/O, no Telegram,
no database. Every function here takes and returns `PIL.Image.Image`, so
tests build tiny in-memory images instead of needing real photos.

v1: `make_birthday_collage`, `../COOKIEBOT-Telegram-Group-Bot/Bot/Birthdays.py:60-79`.
Two things fixed here, not preserved (`.specs/features/util_birthday/spec.md`'s
D-BD-3, `design.md` R3.2/R3.5):

1. **v1 never resized a photo before pasting it.** The canvas is sized from
   only the *first* image's shape, while each image is placed at its *own*
   shape — two differently-sized real photos (the common case, not an edge
   case) makes v1's raw numpy assignment raise. Every image here is resized
   to one fixed cell size before compositing, so the grid math is safe by
   construction.
2. **v1 wrote the result to a hardcoded, non-namespaced local file**
   (`birthday.png`) — the same shape of cross-request race `fun_battle`'s
   D-BT-1 was. Nothing here touches disk; the caller (`cb_worker/jobs/birthday.py`)
   keeps the composited image in memory and uploads the bytes directly.
"""

from __future__ import annotations

import math

from PIL import Image

#: Every photo (and the placeholder) is resized to this square before
#: compositing — design R3.2's `256x256` choice: generous headroom against
#: Telegram's own photo compression, not a value copied from v1 (which never
#: resized at all).
CELL_SIZE = 256


def resize_to_cell(image: Image.Image, size: int = CELL_SIZE) -> Image.Image:
    """`size x size`, `RGBA` — the fix for D-BD-3. `Image.resize` distorts
    rather than crops (matching v1's own casualness about aspect ratio; it
    never handled this at all, so there is no v1 behaviour to preserve here
    beyond "some image ends up in this cell")."""
    return image.convert("RGBA").resize((size, size))


def build_grid(images: list[Image.Image], cell_size: int = CELL_SIZE) -> Image.Image:
    """v1's own grid math (`Birthdays.py:70-71`), unchanged:
    `width = ceil(sqrt(n))`, `height = ceil(n / width)`. Safe here because
    every image is already `cell_size x cell_size` (`resize_to_cell`) —
    placement is a plain `(col * cell_size, row * cell_size)`, no per-image
    shape lookup needed.
    """
    if not images:
        raise ValueError("build_grid requires at least one image")
    n = len(images)
    width = math.ceil(math.sqrt(n))
    height = math.ceil(n / width)
    canvas = Image.new("RGBA", (width * cell_size, height * cell_size))
    for index, image in enumerate(images):
        row, col = divmod(index, width)
        canvas.paste(image, (col * cell_size, row * cell_size))
    return canvas


def overlay_confetti(grid: Image.Image, confetti: Image.Image) -> Image.Image:
    """v1's OpenCV alpha-index-and-replace (`confetti[:, :, -1] == 0` picks
    transparent pixels, backfills them with the collage, `Birthdays.py:76-78`)
    is exactly what `Image.alpha_composite` does natively once both images
    are `RGBA` and the same size — not an approximation, the direct
    equivalent."""
    resized = confetti.convert("RGBA").resize(grid.size)
    return Image.alpha_composite(grid, resized)


def build_collage(images: list[Image.Image], confetti: Image.Image) -> Image.Image:
    """The full pipeline: resize every image, grid them, overlay confetti."""
    cells = [resize_to_cell(image) for image in images]
    grid = build_grid(cells)
    return overlay_confetti(grid, confetti)


__all__ = ["CELL_SIZE", "build_collage", "build_grid", "overlay_confetti", "resize_to_cell"]
