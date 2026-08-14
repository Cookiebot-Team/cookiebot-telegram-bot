"""x_image_search's pure half: v1's search-term extraction and its blocklist.

v1: `qualquer_coisa`,
`../COOKIEBOT-Telegram-Group-Bot/Bot/SocialContent.py:147-170`, and the
blocklist it reads at `:31-33`::

    with open('Static/avoid_search.txt', 'r', encoding='utf-8') as f:
        avoid_search = f.readlines()
    avoid_search = [x.strip() for x in avoid_search]

`Static/avoid_search.txt` is vendored byte-for-byte into
`cb_core/asset_data/search/`, the same treatment `fun_complaint`'s media and
the locale catalogs already get: it is v1 content, not a v2 decision, and
re-deriving its 49 entries by hand would be a rewrite.

## What the blocklist is actually for

`/bash`, `/etc`, `/usr`, `/tmp`, `/proc`, `/root`, `/dev/null`-shaped words,
plus a run of bare punctuation. Nothing about it is moderation — it is v1
noticing that **every unrecognised `/command` becomes an image search**
(`COOKIEBOT.py:283-289`), so a person typing a shell path into a group, or a
stray `/`, would otherwise make the bot post pictures of `/etc`. Ported as-is,
including the entries that look odd out of context (`s`, `q`, `o/`), because
the list is v1's own record of what its users typed by accident.

This module holds no I/O beyond the one file read at import and no Telegram
knowledge; the handler and the worker job are where the feature lives.
"""

from __future__ import annotations

from importlib import resources

_DATA_PACKAGE = "cb_core.asset_data.search"
_BLOCKLIST_FILE = "avoid_search.txt"


def _load_blocklist() -> frozenset[str]:
    raw = resources.files(_DATA_PACKAGE).joinpath(_BLOCKLIST_FILE).read_text(encoding="utf-8")
    return frozenset(line.strip() for line in raw.splitlines() if line.strip())


#: v1's `avoid_search`, read once at import exactly as v1 does (`:31-33`).
AVOID_SEARCH: frozenset[str] = _load_blocklist()


def search_term(text: str) -> str:
    """v1: `msg['text'].split("@")[0].replace("/", ' ')
    .replace("@CookieMWbot", '').replace("@pawstralbot", '')` (`:148`).

    The two `@`-username replacements are dead code in v1 — `split("@")[0]`
    has already removed everything from the first `@` onwards — so they are
    not reproduced. What *is* reproduced is the rest, warts included:

    * every `/` becomes a space, not just the leading one, so `/and/or` is
      searched as `and or`;
    * the split on `@` truncates the query at the first `@` anywhere, so
      `/cat @dog` searches `cat` alone;
    * the leading space the replacement leaves is kept. Google trims it, and
      trimming it here would be a different string from the one v1 sent.
    """
    return text.split("@")[0].replace("/", " ")


def is_avoided(term: str) -> bool:
    """v1: `if searchterm.split()[0] in avoid_search: return` (`:149-150`) —
    the **first word only**, matched exactly, and a silent return.

    v1 raises `IndexError` on a term with no words at all (`"/@x"` reaches
    this with `" "`), which the dispatcher's bare `except` swallows into
    silence; `True` here reaches the same silence without the traceback.
    """
    words = term.split()
    if not words:
        return True
    return words[0] in AVOID_SEARCH


__all__ = ["AVOID_SEARCH", "is_avoided", "search_term"]
