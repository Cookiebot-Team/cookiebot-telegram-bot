"""The publisher's pure logic — caption pipeline, keyboard, price conversion.

Everything here is a transcription of one part of
`../COOKIEBOT-Telegram-Group-Bot/Bot/Publisher.py` with no I/O of its own, so
`cb-gateway` and `cb-worker` can both import it without importing each other
(`.specs/features/util_postforwarder/design.md` R3.2). The one function that
needs a network — the exchange-rate lookup — takes it as a callable.

Nothing in this module knows about aiogram; `resolve_pending_media` takes the
handful of fields it needs rather than a `Message`, so the unit tests do not
have to build Telegram objects to exercise a dict lookup.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence

from cb_core.pending_posts import PendingPost

# v1's own pattern (`Publisher.py:23`), verbatim. One capturing group, so
# `findall` yields the whole URL.
URL_REGEX = re.compile(r"\b((?:https?|ftp|file):\/\/[-a-zA-Z0-9+&@#\/%?=~_|!:,.;]{1,2048})")

# `Publisher.py:24` — the module redefines this rather than using the one it
# imported from GroupShield, so this is the pattern that actually runs.
EMOJI_PATTERN = re.compile(
    "["
    "\U0001f600-\U0001f64f"
    "\U0001f300-\U0001f5ff"
    "\U0001f680-\U0001f6ff"
    "\U0001f700-\U0001f77f"
    "\U0001f780-\U0001f7ff"
    "\U0001f800-\U0001f8ff"
    "\U0001f900-\U0001f9ff"
    "\U0001fa00-\U0001fa6f"
    "\U0001fa70-\U0001faff"
    "\U00002702-\U000027b0"
    "\U000024c2-\U0001f251"
    "]+"
)

# `universal_funcs.py:353-356`.
_KEYCAP_DIGITS = {
    "0️⃣": "0",
    "1️⃣": "1",
    "2️⃣": "2",
    "3️⃣": "3",
    "4️⃣": "4",
    "5️⃣": "5",
    "6️⃣": "6",
    "7️⃣": "7",
    "8️⃣": "8",
    "9️⃣": "9",
}
_KEYCAP_PATTERN = re.compile("|".join(map(re.escape, _KEYCAP_DIGITS)))

# The same table the other way round (`universal_funcs.py:346-351`), derived
# rather than written out twice.
_DIGIT_KEYCAPS = {digit: keycap for keycap, digit in _KEYCAP_DIGITS.items()}

# `Publisher.py:146-163`. Ported as written, including the two quirks below.
_CURRENCY_CODES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("$", "US$", "USD", "U$"), "USD"),
    (("€", "EUR"), "EUR"),
    (("£", "GBP"), "GBP"),
    (("R$", "BRL"), "BRL"),
    (("¥", "JPY"), "JPY"),
    (("C$", "CAD"), "CAD"),
    (("A$", "AUD"), "AUD"),
)

#: Captions are truncated to this before sending (`Publisher.py:204-205`).
CAPTION_LIMIT = 1020

#: The three substitutions v1 applies after conversion (`Publisher.py:202-203`),
#: so a caption's stray angle brackets do not collide with `parse_mode='HTML'`.
#: The replacements are deliberately the look-alike characters v1 chose, so the
#: rendered caption is byte-identical to v1's: U+2A7D, U+2A7E and U+FF06.
_HTML_SAFE = (("<", "⩽"), (">", "⩾"), ("&", "＆"))  # noqa: RUF001 - the look-alikes are the point


# ------------------------------------------------------------------ media resolution


def resolve_pending_media(
    *,
    photo_file_id: str | None = None,
    video_file_id: str | None = None,
    animation_file_id: str | None = None,
    document_file_id: str | None = None,
) -> tuple[str, str] | None:
    """v1's `add_post_to_cache` media branch (`:27-38`) — `(kind, file_id)`.

    First match wins, in v1's order. A **document is reported as `animation`**
    (`:36-38`): v1 files it under that key and later re-sends it with
    `sendAnimation`, which works because these ads are GIFs. Preserved
    (D-PF-4) — "fixing" it would change how every document ad is delivered.

    `None` when the message carries none of the four. v1 has no such branch and
    raises `UnboundLocalError` instead; the caller's filter already guarantees
    one is present, so this is a total function rather than a behaviour change.
    """
    if photo_file_id:
        return "photo", photo_file_id
    if video_file_id:
        return "video", video_file_id
    if animation_file_id:
        return "animation", animation_file_id
    if document_file_id:
        return "animation", document_file_id
    return None


# --------------------------------------------------------------------- text helpers


def emojis_to_numbers(text: str) -> str:
    """Keycap emoji to the ASCII digit (`universal_funcs.py:353-356`)."""
    return _KEYCAP_PATTERN.sub(lambda m: _KEYCAP_DIGITS[m.group()], text)


def number_to_emojis(number: int) -> str:
    """The inverse: every digit of `number` as its keycap emoji
    (`universal_funcs.py:346-351`).

    v1 keeps both directions in the same module and this port keeps them in
    the same place for the same reason — one keycap table, not two. It lives
    in `publisher.py` because that is where the table already was, even
    though its only caller today is `fun_partneredcons`' countdown caption
    (`Miscellaneous.py:274` and its four siblings); moving the table to a new
    shared module for one function would be the second way to do something
    that already has one.

    v1 indexes a dict per character and would raise `KeyError` on a minus
    sign. Unreachable there (`daysremaining` is looped up past `-5` before
    any caption is built) and unreachable here for the same reason, so the
    signature takes `int` and formats `abs()`-free: a negative input would
    render `-` unchanged rather than crash, which is strictly less bad and
    changes nothing observable.
    """
    return "".join(_DIGIT_KEYCAPS.get(character, character) for character in str(number))


def remove_emojis_from_ends(value: str) -> str:
    """`Publisher.py:175-180`. Strips leading, then trailing, emoji runs.

    v1 tests the *reversed* string for the trailing pass, which means a
    multi-codepoint emoji is consumed one codepoint at a time from the right.
    Same loop here, same result.
    """
    while value and EMOJI_PATTERN.match(value):
        value = value[1:]
    while value and EMOJI_PATTERN.match(value[::-1]):
        value = value[:-1]
    return value


def extract_caption_urls(caption: str) -> list[str]:
    """Unique URLs in the caption, in first-appearance order.

    v1 iterates `set(re.findall(URL_REGEX, caption))` (`:186`). A set of strings
    has no defined iteration order across processes — Python randomises string
    hashing per interpreter — so v1's ad buttons genuinely appear in a different
    order after every restart. Deduplicating in first-appearance order keeps the
    same *set* of buttons and makes their order a property of the caption
    instead of the process. Recorded as a deliberate divergence.
    """
    return list(dict.fromkeys(URL_REGEX.findall(caption)))


def finalise_caption(text: str) -> str:
    """`Publisher.py:202-205`: the three substitutions, then the 1020 cap."""
    for old, new in _HTML_SAFE:
        text = text.replace(old, new)
    return text[:CAPTION_LIMIT]


# ------------------------------------------------------------------ price conversion


def _currency_code(currency: str) -> str:
    for symbols, code in _CURRENCY_CODES:
        if currency in symbols:
            return code
    # `Publisher.py:160-161` writes `elif currency in ('ARS')` — a bare string,
    # not a tuple, so this is a *substring* test: "A", "R", "S", "AR" and "RS"
    # all match it. price_parser never emits any of those, so the branch is
    # unreachable in practice, but it is ported as written rather than silently
    # corrected to a tuple.
    if currency in "ARS":
        return "ARS"
    return currency


def convert_prices_in_text(
    text: str,
    code_target: str,
    rate_lookup: Callable[[str, str], float | None],
) -> str:
    """`Publisher.py:129-173`, with the network call injected.

    Per paragraph: the **largest** amount any word parses to, and the **last**
    currency any word parses to — not the currency attached to that amount.
    That is v1's arithmetic and it is what decides the number in every
    converted ad, so it is reproduced rather than improved.

    `rate_lookup(code_from, code_target)` returns the rate, or `None` for
    "could not find out" — which yields the paragraph unchanged, exactly as
    v1's `except Exception` does (`:171-172`).
    """
    from price_parser import Price  # local: keeps import cost off every cb_core import

    if code_target == "BRL" and any(x in text for x in ("R$", "BRL", "Reais", "reais")):
        return text

    text = text.replace("Reais", "R$").replace("reais", "R$")
    final_text = ""
    for paragraph in text.split("\n"):
        amount = 0.0
        currency: str | None = None
        for word in paragraph.split():
            parsed = Price.fromstring(word, currency_hint="usd")
            if parsed.amount is not None and parsed.amount_float is not None:
                amount = max(amount, parsed.amount_float)
            if parsed.currency is not None:
                currency = parsed.currency
        if amount == 0 or currency is None:
            final_text += f"{paragraph}\n"
            continue

        code_from = _currency_code(currency)
        if code_from == code_target:
            # D-PF-6, preserved: v1 returns the *whole* text here, discarding
            # every conversion already appended to earlier paragraphs. A pure
            # output quirk with no correctness or safety impact — and changing
            # it would rewrite the caption of every mixed-currency ad.
            return text

        rate = rate_lookup(code_from, code_target)
        if rate is None:
            final_text += f"{paragraph}\n"
            continue
        final_text += f"{paragraph} ({code_target} ≈{round(amount * rate, 2)})\n"
    return final_text


# ---------------------------------------------------------------------- the keyboard


class PostButton:
    """One inline-keyboard row. Not an aiogram type: `cb_core` must not depend
    on the bot framework, and both callers build their own markup from these."""

    __slots__ = ("text", "url")

    def __init__(self, text: str, url: str) -> None:
        self.text = text
        self.url = url

    def __eq__(self, other: object) -> bool:
        return isinstance(other, PostButton) and other.text == self.text and other.url == self.url

    def __hash__(self) -> int:
        return hash((self.text, self.url))

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"PostButton({self.text!r}, {self.url!r})"


def build_post_keyboard(
    *,
    caption: str,
    caption_entity_urls: Sequence[str],
    origin_title: str,
    origin_username: str | None,
    author_first_name: str | None,
    author_username: str | None,
    postmail_chat_link: str,
    hidden_author_names: Iterable[str] = (),
) -> tuple[list[PostButton], str]:
    """`prepare_post`'s keyboard (`:184-199`), and the caption it rewrites.

    Returns `(buttons, caption)`: v1 edits the caption in place while building
    the keyboard, replacing each URL with its de-emojified form (`:191`), so the
    two results are inseparable.

    Row order is v1's and load-bearing — the reply relay finds a campaign by
    reading row 0's text back off the button (`:361`), so the origin channel
    must stay first.
    """
    buttons = [PostButton(origin_title, f"https://t.me/{origin_username}")]
    origin_link = f"https://t.me/{origin_username}"

    for url in extract_caption_urls(caption):
        name = url.rstrip("/").split("/")[-1].replace("www.", "")
        clean = remove_emojis_from_ends(url)
        if name and len(clean) > 3 and clean != origin_link:
            buttons.append(PostButton(name, clean))
            caption = caption.replace(url, clean)

    for entity_url in caption_entity_urls:
        # v1 caps the *whole* keyboard at 5 rows here, not the entity buttons —
        # so how many entity links survive depends on how many URL buttons the
        # caption already produced (`:194`).
        if len(entity_url) > 3 and len(buttons) < 5:
            name = (
                entity_url.rstrip("/")
                .replace("www.", "")
                .replace("http://", "")
                .replace("https://", "")
            )
            buttons.append(PostButton(name, entity_url))

    if author_first_name is not None and not any(
        hidden in author_first_name for hidden in hidden_author_names
    ):
        # v1: `'Mekhy' not in origin_user['first_name']` (`:197`) — a substring
        # test, not equality, so "Mekhyw" is hidden too. Kept as one (D-PF-10);
        # the name list is now configuration.
        buttons.append(PostButton(author_first_name, f"https://t.me/{author_username}"))

    buttons.append(PostButton("Mural 📬", postmail_chat_link))
    return buttons, caption


def pending_post_from(
    kind_and_id: tuple[str, str], caption: str, entity_urls: Sequence[str]
) -> PendingPost:
    """Assemble the cache entry `add_post_to_cache` wrote (`:39-44`)."""
    kind, file_id = kind_and_id
    return PendingPost(
        media_kind=kind,
        file_id=file_id,
        caption=caption,
        caption_entity_urls=tuple(entity_urls),
    )
