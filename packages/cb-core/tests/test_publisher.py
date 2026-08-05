"""Unit coverage for `cb_core.publisher` — the pure half of v1's `prepare_post`.

Every assertion here is a transcription of a specific line of
`../COOKIEBOT-Telegram-Group-Bot/Bot/Publisher.py`; the file:line is in each
test's name or docstring. Contract: `docs/contracts/util_postforwarder.md`.
"""

from __future__ import annotations

import pytest

from cb_core import publisher

# ------------------------------------------------------------------ media resolution


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"photo_file_id": "p"}, ("photo", "p")),
        ({"video_file_id": "v"}, ("video", "v")),
        ({"animation_file_id": "a"}, ("animation", "a")),
        # v1 `:36-38` files a document under the `animation` key and later
        # re-sends it with sendAnimation. D-PF-4, preserved.
        ({"document_file_id": "d"}, ("animation", "d")),
        ({}, None),
    ],
)
def test_resolve_pending_media(kwargs: dict[str, str], expected: tuple[str, str] | None) -> None:
    assert publisher.resolve_pending_media(**kwargs) == expected


def test_photo_wins_over_every_other_kind() -> None:
    """v1's branch order (`:27-38`) is photo, video, animation, document."""
    assert publisher.resolve_pending_media(
        photo_file_id="p", video_file_id="v", document_file_id="d"
    ) == ("photo", "p")


# --------------------------------------------------------------------- text helpers


def test_emojis_to_numbers() -> None:
    """`universal_funcs.py:353-356`."""
    assert publisher.emojis_to_numbers("only 1️⃣0️⃣ left") == "only 10 left"


def test_emojis_to_numbers_leaves_other_emoji_alone() -> None:
    assert publisher.emojis_to_numbers("🔥 hot 5️⃣") == "🔥 hot 5"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("🔥https://x.com/a🔥", "https://x.com/a"),
        ("https://x.com/a", "https://x.com/a"),
        ("🔥🔥🔥", ""),
        ("", ""),
    ],
)
def test_remove_emojis_from_ends(value: str, expected: str) -> None:
    """`Publisher.py:175-180`. The all-emoji case would loop forever on v1's
    own code once the string empties (`input_string[::-1]` of `''` is `''`,
    which `match` returns None for) — it terminates there too, but only by
    accident of that None; the explicit `while value` here is the same result."""
    assert publisher.remove_emojis_from_ends(value) == expected


def test_extract_caption_urls_dedupes_in_first_appearance_order() -> None:
    """v1 iterates a `set` (`:186`), whose order is randomised per process.

    Same set of URLs, deterministic order — a deliberate divergence, recorded
    in the spec.
    """
    caption = "see https://b.io/y then https://a.com/x and https://b.io/y again"
    assert publisher.extract_caption_urls(caption) == ["https://b.io/y", "https://a.com/x"]


def test_extract_caption_urls_ignores_non_urls() -> None:
    assert publisher.extract_caption_urls("no links here, just text") == []


def test_finalise_caption_substitutes_and_truncates() -> None:
    """`Publisher.py:202-205`."""
    assert publisher.finalise_caption("a<b>c&d") == "a⩽b⩾c＆d"  # noqa: RUF001 - look-alikes are the point
    assert len(publisher.finalise_caption("x" * 2000)) == publisher.CAPTION_LIMIT


# ------------------------------------------------------------------ price conversion


def _rate(_from: str, _to: str) -> float | None:
    return 5.0


def _no_rate(_from: str, _to: str) -> float | None:
    return None


def test_converts_and_appends_v1s_suffix() -> None:
    out = publisher.convert_prices_in_text("cheap $25 now", "BRL", _rate)
    assert out == "cheap $25 now (BRL ≈125.0)\n"


def test_largest_amount_wins_within_a_paragraph() -> None:
    """`:139-140` keeps the maximum, not the first or the last."""
    out = publisher.convert_prices_in_text("$5 or $30 or $12", "BRL", _rate)
    assert "(BRL ≈150.0)" in out


def test_paragraph_without_a_price_is_untouched() -> None:
    out = publisher.convert_prices_in_text("no price here\n$10", "BRL", _rate)
    assert out.splitlines()[0] == "no price here"


def test_rate_lookup_failure_leaves_the_paragraph_alone() -> None:
    """v1's per-paragraph `except Exception` (`:171-172`)."""
    assert publisher.convert_prices_in_text("cheap $25", "BRL", _no_rate) == "cheap $25\n"


def test_brl_target_short_circuits_when_the_text_already_mentions_reais() -> None:
    """`:130-131` — the whole text is returned unmodified, no `\\n` appended."""
    assert publisher.convert_prices_in_text("R$99 stuff", "BRL", _rate) == "R$99 stuff"


def test_reais_is_rewritten_to_the_symbol_before_parsing() -> None:
    """`:133`. The short-circuit above fires first for a BRL target, so this is
    only observable against a different target."""
    out = publisher.convert_prices_in_text("50 reais", "USD", _rate)
    assert "R$" in out


def test_same_currency_returns_the_whole_text_discarding_earlier_work() -> None:
    """D-PF-6, preserved (`:164-165`).

    The first paragraph converts, the second is already in the target currency,
    and v1 answers with the *original* text — throwing away the conversion it
    just made. Asserted rather than fixed, because fixing it would rewrite the
    caption of every mixed-currency ad.
    """
    out = publisher.convert_prices_in_text("£10 first\n$20 second", "USD", _rate)
    assert out == "£10 first\n$20 second"
    assert "≈" not in out


# ---------------------------------------------------------------------- the keyboard


def _keyboard(**overrides: object) -> tuple[list[publisher.PostButton], str]:
    kwargs: dict[str, object] = {
        "caption": "",
        "caption_entity_urls": (),
        "origin_title": "FurShop",
        "origin_username": "furshop",
        "author_first_name": None,
        "author_username": None,
        "postmail_chat_link": "https://t.me/Mural",
        "hidden_author_names": ("Mekhy",),
    }
    kwargs.update(overrides)
    return publisher.build_post_keyboard(**kwargs)  # type: ignore[arg-type] # test kwargs


def test_origin_channel_is_always_the_first_row() -> None:
    """`:185`, and load-bearing: the reply relay reads this button's text back
    off the message to find the campaign (`:361`)."""
    buttons, _ = _keyboard()
    assert buttons[0] == publisher.PostButton("FurShop", "https://t.me/furshop")


def test_mural_is_always_the_last_row() -> None:
    """`:199`."""
    buttons, _ = _keyboard()
    assert buttons[-1] == publisher.PostButton("Mural 📬", "https://t.me/Mural")


def test_caption_urls_become_buttons_named_after_their_last_path_segment() -> None:
    """`:186-191`."""
    buttons, _ = _keyboard(caption="buy at https://shop.com/deal")
    assert publisher.PostButton("deal", "https://shop.com/deal") in buttons


def test_a_url_pointing_at_the_origin_channel_is_not_repeated() -> None:
    """`:189` excludes it — row 1 already links there."""
    buttons, _ = _keyboard(caption="join https://t.me/furshop")
    assert [b for b in buttons if b.url == "https://t.me/furshop"] == [buttons[0]]


def test_the_caption_is_rewritten_to_the_de_emojified_url() -> None:
    """`:191` — the keyboard build mutates the caption, so both come back."""
    _, caption = _keyboard(caption="buy at https://shop.com/deal🔥 now")
    assert "https://shop.com/deal" in caption


def test_entity_urls_stop_once_the_keyboard_reaches_five_rows() -> None:
    """`:194` caps the *whole* keyboard at 5, not the entity buttons — so how
    many entity links survive depends on how many the caption already made."""
    buttons, _ = _keyboard(
        caption="a https://one.com/a b https://two.com/b c https://three.com/c",
        caption_entity_urls=("https://four.com/d", "https://five.com/e"),
    )
    urls = [b.url for b in buttons]
    assert "https://four.com/d" in urls
    assert "https://five.com/e" not in urls


def test_short_entity_urls_are_skipped() -> None:
    """`:194` requires `len(entity['url']) > 3`."""
    buttons, _ = _keyboard(caption_entity_urls=("ab",))
    assert [b for b in buttons if b.url == "ab"] == []


def test_the_author_gets_a_button() -> None:
    """`:197-198`."""
    buttons, _ = _keyboard(author_first_name="Ana", author_username="ana")
    assert publisher.PostButton("Ana", "https://t.me/ana") in buttons


def test_a_hidden_author_name_suppresses_the_button() -> None:
    """v1 hardcodes `'Mekhy' not in first_name` (`:197`), a substring test — so
    "Mekhyw" is hidden too. D-PF-10 turns the name into configuration and keeps
    the substring semantics."""
    buttons, _ = _keyboard(author_first_name="Mekhyw", author_username="mekhyw")
    assert all("mekhyw" not in b.url for b in buttons)


def test_no_author_means_no_author_button() -> None:
    """v1's `origin_user is not None` (`:197`) — `getChatMember` failed."""
    buttons, _ = _keyboard()
    assert len(buttons) == 2  # origin + Mural
