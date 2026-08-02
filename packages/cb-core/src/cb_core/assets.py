"""Static asset accessor for bot-owned media (photos, audio) shipped with cb-core.

v1 opened files from a relative `Bot/Static/` path at send time
(`../COOKIEBOT-Telegram-Group-Bot/Bot/Miscellaneous.py:241-259`); that path does
not exist once this code ships as an installed wheel instead of a checkout. This
module resolves the same bytes through `importlib.resources`, the same idiom
`cb_core/locales.py` uses for `locale_data`.

This is the only asset accessor for bot-owned static media — `fun_death` and
`fun_meme` are expected to reuse `path`/`pool` rather than growing a second one
(design R1.2). These assets are never user-supplied, so they do not go through
`cb_core.storage` (AGENTS.md §5 governs user content, not bot fixtures).
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

_DATA_PACKAGE = "cb_core.asset_data"


def path(*parts: str) -> Path:
    """Resolve `parts` under the installed `asset_data` package to a filesystem Path.

    `importlib.resources.files` is what still finds the file once the package is
    installed from a wheel rather than run from a source checkout.
    """
    traversable = resources.files(_DATA_PACKAGE).joinpath(*parts)
    return Path(str(traversable))


def pool(*parts: str, suffix: str) -> tuple[Path, ...]:
    """Every file directly under `parts` whose extension is `suffix`, sorted.

    Sorted so `rng.choice(pool(...))` is reproducible under a seeded rng in
    tests — `os.listdir` order (what v1's equivalent relied on) is not
    guaranteed across platforms or filesystems.
    """
    directory = path(*parts)
    return tuple(sorted(entry for entry in directory.iterdir() if entry.suffix == suffix))
