"""Unit coverage for `cb_gateway.handlers.giveaway`'s pure surface.

No dispatcher, no Telegram, no database: the trigger, the callback grammar,
the keyboards, the winner draw and the catalog lookups (including the two
places v1's `es` file is missing the key). The stateful half is covered by
`qa/test_x_giveaways.py` and `qa/integration/test_giveaways.py`; see
`docs/contracts/x_giveaways.md`.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

import pytest

from cb_core.giveaways import Participant
from cb_gateway.filters import CommandName
from cb_gateway.handlers.giveaway import (
    CALLBACK_PREFIX,
    WINNER_CHOICES,
    announcement_text,
    button_labels,
    display_name_for,
    draw_more_keyboard,
    entry_keyboard,
    gtext,
    parse_callback_data,
    pick_winners,
    winner_caption,
    winner_keyboard,
)


@dataclass
class _FakeMessage:
    text: str | None


# ------------------------------------------------------------------- triggers


@pytest.mark.asyncio
async def test_giveaway_resolves() -> None:
    result = await CommandName("giveaway")(
        _FakeMessage("/giveaway a fursuit"), bot_username="CookieMWbot"
    )
    assert result is not False


@pytest.mark.asyncio
async def test_addressed_at_a_different_bot_does_not_resolve() -> None:
    result = await CommandName("giveaway")(
        _FakeMessage("/giveaway@SomeOtherBot a fursuit"), bot_username="CookieMWbot"
    )
    assert result is False


# ------------------------------------------------------------- callback grammar


@pytest.mark.parametrize(
    ("data", "action", "token"),
    [
        ("GIVEAWAY 1 abc-123", "1", "abc-123"),
        ("GIVEAWAY 5 abc-123", "5", "abc-123"),
        ("GIVEAWAY enter", "enter", ""),
        ("GIVEAWAY end", "end", ""),
        ("GIVEAWAY delete", "delete", ""),
        # v1 tolerates an unknown action and answers its own error string,
        # so the parser has to surface it rather than reject it.
        ("GIVEAWAY nonsense", "nonsense", ""),
    ],
)
def test_parses_v1_callback_data(data: str, action: str, token: str) -> None:
    press = parse_callback_data(data)
    assert press is not None
    assert (press.action, press.token) == (action, token)


@pytest.mark.parametrize("data", ["", "GIVEAWAY", "CONFIG a -100", "RULES pt", "giveaway enter"])
def test_ignores_other_callbacks(data: str) -> None:
    assert parse_callback_data(data) is None


# ------------------------------------------------------------------- keyboards


def test_winner_keyboard_offers_v1s_five_counts() -> None:
    rows = winner_keyboard("01931f00-dead-7000-8000-0123456789ab").inline_keyboard
    assert [button.text for (button,) in rows] == [str(n) for n in WINNER_CHOICES]
    assert [button.callback_data for (button,) in rows] == [
        f"{CALLBACK_PREFIX} {n} 01931f00-dead-7000-8000-0123456789ab" for n in WINNER_CHOICES
    ]


def test_callback_payload_fits_telegrams_64_byte_cap() -> None:
    """v1 truncated the prize to 20 characters to stay inside it (`:36`); the
    token is fixed-width, so the payload cannot grow with the prize."""
    from cb_core.pending_giveaways import new_token

    for (button,) in winner_keyboard(new_token()).inline_keyboard:
        assert len((button.callback_data or "").encode()) <= 64


def test_entry_keyboard_labels_come_from_the_catalog() -> None:
    rows = entry_keyboard("pt").inline_keyboard
    assert rows[0][0].text == "Quero Entrar!"
    assert rows[0][0].callback_data == "GIVEAWAY enter"
    assert rows[1][0].callback_data == "GIVEAWAY end"


def test_draw_more_keyboard_is_the_two_emoji() -> None:
    rows = draw_more_keyboard().inline_keyboard
    assert [button.text for (button,) in rows] == ["✅", "❌"]
    assert [button.callback_data for (button,) in rows] == ["GIVEAWAY end", "GIVEAWAY delete"]


# --------------------------------------------------------------------- catalog


def test_es_falls_back_per_key_not_per_object() -> None:
    """v1's `es` catalog has a `giveaway` object but is missing ten of its
    entries. An object-level fallback (`_captcha_strings`'s shape) would still
    answer a Spanish group with a key name for those; a per-key one answers in
    English, which is what `locales.get` does for every flat key."""
    assert gtext("es", "create").startswith("¡Vamos a crear")  # present in es
    assert gtext("es", "not_found") == "Giveaway not found"  # missing -> en
    assert gtext("es", "no_one") == "No participants in the giveaway!"


def test_button_labels_fall_back_to_english_for_es() -> None:
    assert button_labels("es") == ("Put me in!", "ADMINS: End Giveaway")


def test_unknown_key_returns_the_key() -> None:
    assert gtext("en", "does_not_exist") == "giveaway.does_not_exist"


def test_announcement_carries_prize_and_count() -> None:
    text = announcement_text("en", "Fursuit of Mekhy 🐾🦝", 3)
    assert "Fursuit of Mekhy 🐾🦝" in text
    assert "3" in text
    # v1 never truncated the *announcement*, only the callback payload it
    # rebuilt the prize from (D-GA-1) — nothing here is cut at 20 characters.
    assert "…" not in text


# ------------------------------------------------------------------- the draw


def test_display_name_prefers_the_username() -> None:
    assert display_name_for(7, "tester", "Test") == "@tester"
    assert display_name_for(7, None, "Test") == "Test"
    assert display_name_for(7, None, None) == "7"


def test_draw_is_capped_by_the_number_of_entrants() -> None:
    entrants = (Participant(1, "@a"), Participant(2, "@b"))
    assert len(pick_winners(entrants, 5, random.Random(0))) == 2


def test_draw_never_repeats_a_winner_within_one_draw() -> None:
    entrants = tuple(Participant(i, f"@u{i}") for i in range(10))
    winners = pick_winners(entrants, 4, random.Random(1))
    assert len({winner.user_id for winner in winners}) == 4


def test_no_entrants_draws_nobody() -> None:
    assert pick_winners((), 3, random.Random(0)) == []


def test_singular_caption_is_chosen_by_the_configured_count_not_the_drawn_one() -> None:
    """v1 keys off `n_winners`, the number the admin picked, even when fewer
    people entered (`Giveaways.py:131`) — so a 3-winner raffle with a single
    entrant still reads "our 1st winner is…", not "we have a winner!"."""
    one = winner_caption("en", index=1, winner="@a", prize="cake", total=1)
    more = winner_caption("en", index=1, winner="@a", prize="cake", total=3)
    assert one.startswith("We have a winner!")
    assert more.startswith("Our 1 winner is")


def test_caption_substitutes_in_every_language() -> None:
    for lang in ("en", "pt", "es"):
        caption = winner_caption(lang, index=2, winner="@bob", prize="cake", total=3)
        assert "@bob" in caption
        assert "cake" in caption
        assert "%(" not in caption
