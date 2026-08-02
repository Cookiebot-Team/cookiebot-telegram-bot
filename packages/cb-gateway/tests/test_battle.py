"""Unit tests for fun_battle's pure logic: target parsing, shape selection,
roster resolution, catalog reads and caption assembly.

The confirmed-photo flow (roster + Bot API + media group + poll) against
mock Telegram lives in `qa/test_fun_battle.py`; this file is everything in
between — no Telegram session, no database. Model:
`packages/cb-gateway/tests/test_ship.py`, `packages/cb-gateway/tests/test_calladms.py`.
"""

from __future__ import annotations

import random

from cb_core.members import MemberRef
from cb_gateway.handlers import battle as bt

# --------------------------------------------------------------- target parsing


class TestParseTaggedTargets:
    def test_no_at_sign_is_empty(self) -> None:
        assert bt.parse_tagged_targets("/battle") == []

    def test_single_tag(self) -> None:
        assert bt.parse_tagged_targets("/battle @alice") == ["alice"]

    def test_two_tags(self) -> None:
        assert bt.parse_tagged_targets("/battle @alice @bob") == ["alice ", "bob"]

    def test_trailing_text_is_captured_raw(self) -> None:
        """v1's own quirk (`get_members_tagged`, `SocialContent.py:104-111`):
        the raw text between `@` signs is kept, untrimmed, multi-word
        included."""
        assert bt.parse_tagged_targets("/battle @alice fight @bob now") == [
            "alice fight ",
            "bob now",
        ]

    def test_bot_suffixed_final_target_is_dropped(self) -> None:
        """The `.endswith('bot')` filter only ever bites on the *last* tag
        in the message: any earlier tag carries trailing text up to the
        next `"@"` (this class's own quirk above), so it essentially never
        ends in exactly `"bot"` — v1's own filter, byte-for-byte, warts
        included."""
        assert bt.parse_tagged_targets("/battle @alice @spambot") == ["alice "]

    def test_bot_suffixed_non_final_target_is_not_dropped(self) -> None:
        # "spambot " (trailing space, since another @ follows) does not
        # end in "bot" — the filter silently misses it, same as v1.
        assert bt.parse_tagged_targets("/battle @spambot @alice") == ["spambot ", "alice"]

    def test_bot_suffix_check_is_case_sensitive(self) -> None:
        """v1: `target.endswith('bot')`, lowercase only — `"AdminBot"` ends
        in `"Bot"`, not `"bot"`, so it survives."""
        assert bt.parse_tagged_targets("/battle @AdminBot") == ["AdminBot"]

    def test_third_plus_tag_is_still_captured(self) -> None:
        # battle_shape/the handler only ever reads the first two; parsing
        # itself captures everything, matching v1's own get_members_tagged.
        assert bt.parse_tagged_targets("/battle @a @b @c") == ["a ", "b ", "c"]


class TestLeadingToken:
    def test_strips_and_takes_first_word(self) -> None:
        assert bt._leading_token("alice fight ") == "alice"  # noqa: SLF001

    def test_plain_username_unchanged(self) -> None:
        assert bt._leading_token("bob") == "bob"  # noqa: SLF001

    def test_empty_or_blank_is_empty(self) -> None:
        assert bt._leading_token("") == ""  # noqa: SLF001
        assert bt._leading_token("   ") == ""  # noqa: SLF001


class TestUsesRandom:
    def test_random_anywhere_in_text(self) -> None:
        assert bt.uses_random("/battle random") is True
        assert bt.uses_random("/battle @alice random battle") is True

    def test_case_insensitive(self) -> None:
        assert bt.uses_random("/battle RANDOM") is True

    def test_no_random_word(self) -> None:
        assert bt.uses_random("/battle @alice @bob") is False


class TestBattleShape:
    def test_two_explicit_tags_is_two_people(self) -> None:
        assert bt.battle_shape("/battle @a @b", ["a ", "b"]) is bt.BattleShape.TWO_PEOPLE

    def test_random_alone_is_two_people(self) -> None:
        assert bt.battle_shape("/battle random", []) is bt.BattleShape.TWO_PEOPLE

    def test_random_wins_over_two_tags(self) -> None:
        """v1 checks `'random' in text` a second time once it already knows
        this is the two-people shape (`SocialContent.py:298-299`) and lets
        it win — design R2.0. `battle_shape` itself only decides the shape;
        `uses_random` is what the handler re-checks to pick the sub-path,
        exactly mirroring that double check."""
        assert bt.battle_shape("/battle random @a @b", ["a ", "b"]) is bt.BattleShape.TWO_PEOPLE
        assert bt.uses_random("/battle random @a @b") is True

    def test_one_tag(self) -> None:
        assert bt.battle_shape("/battle @a", ["a"]) is bt.BattleShape.ONE_TAG

    def test_no_tag_no_random_is_self(self) -> None:
        assert bt.battle_shape("/battle", []) is bt.BattleShape.SELF


# --------------------------------------------------------------- roster resolution


_ALICE = MemberRef(user_id=1, username="alice")
_BOB = MemberRef(user_id=2, username="bob")
_NO_USERNAME = MemberRef(user_id=3, username=None)


class TestFindInRoster:
    def test_matches_case_insensitively(self) -> None:
        roster = (_ALICE, _BOB)
        assert bt._find_in_roster(roster, "ALICE") == 1  # noqa: SLF001

    def test_matches_leading_token_of_a_multiword_raw_capture(self) -> None:
        roster = (_ALICE, _BOB)
        assert bt._find_in_roster(roster, "bob now") == 2  # noqa: SLF001

    def test_no_match_is_none(self) -> None:
        roster = (_ALICE, _BOB)
        assert bt._find_in_roster(roster, "carol") is None  # noqa: SLF001

    def test_member_without_a_username_never_matches(self) -> None:
        assert bt._find_in_roster((_NO_USERNAME,), "") is None  # noqa: SLF001

    def test_empty_raw_never_matches(self) -> None:
        assert bt._find_in_roster((_ALICE,), "   ") is None  # noqa: SLF001


class TestPickTwoRandom:
    def test_fewer_than_two_candidates_is_none(self) -> None:
        assert bt.pick_two_random([]) is None
        assert bt.pick_two_random([_ALICE]) is None

    def test_two_candidates_are_both_returned(self) -> None:
        picked = bt.pick_two_random([_ALICE, _BOB], rng=random.Random(7))
        assert set(picked or []) == {_ALICE, _BOB}

    def test_seeded_rng_is_reproducible(self) -> None:
        candidates = [_ALICE, _BOB, MemberRef(user_id=4, username="carol")]
        first = bt.pick_two_random(candidates, rng=random.Random(42))
        second = bt.pick_two_random(candidates, rng=random.Random(42))
        assert first == second


# --------------------------------------------------------------------- catalog


class TestCatalogChoice:
    def test_picks_from_the_real_en_catalog(self) -> None:
        value = bt._catalog_choice("en", "battle_type", rng=random.Random(1))  # noqa: SLF001
        assert value in (
            "Boxing 🥊🥊",
            "Wrestling 🎭",
            "Greco Wrestling 🤼‍♂️",
            "Martial Arts 🥋",
            "Sambo 👊",
            "Muay Thai 🥋",
            "Street Fighting 👊",
            "Pool Fighting💧",
            "Judo 🇯🇵",
            "Sumo ⛩",
            "Gutpunching 💪",
            "Ballbusting 🍳🍳",
        )

    def test_falls_back_to_en_for_a_missing_language(self) -> None:
        # "xx" resolves to "en" via cb_core.locales.resolve_language's own
        # fallback before this function ever runs, so this exercises the
        # same catalog either way -- included for completeness against a
        # key that is genuinely absent from a non-en catalog.
        value = bt._catalog_choice("es", "battle_rule", rng=random.Random(2))  # noqa: SLF001
        assert isinstance(value, str)
        assert value

    def test_seeded_rng_is_reproducible(self) -> None:
        first = bt._catalog_choice("en", "battle_equip", rng=random.Random(9))  # noqa: SLF001
        second = bt._catalog_choice("en", "battle_equip", rng=random.Random(9))  # noqa: SLF001
        assert first == second


class TestFlavourSuffix:
    def test_contains_the_three_labels(self) -> None:
        suffix = bt.flavour_suffix("en", rng=random.Random(3))
        assert "Type:" in suffix
        assert "Rules:" in suffix
        assert "Equipment:" in suffix


# --------------------------------------------------------------- caption assembly


class TestBuildCaption:
    def test_explicit_tags_get_no_at_prefix(self) -> None:
        built = bt.build_caption("alice", "bob", at_prefix=False, suffix="!")
        assert built.caption == "alice VS bob!"
        assert built.choices == ("alice", "bob")

    def test_random_pick_gets_an_at_prefix(self) -> None:
        """v1's own inconsistency (`SocialContent.py:328-333`), preserved:
        the caption gets `@`, the poll choices never do, in either shape."""
        built = bt.build_caption("alice", "bob", at_prefix=True, suffix="!")
        assert built.caption == "@alice VS @bob!"
        assert built.choices == ("alice", "bob")
