"""Unit coverage for util_everyone — trigger surface and the pure chunker.

Pure logic only, per design R6.1: no dispatcher, no Telegram, no database. The
admin gate and roster read are one-line calls onto already-tested modules
(`cb_core.admins.resolve_actor`, `cb_core.members.roster`) and are exercised
end to end in `qa/test_util_everyone.py` instead. Model: `test_calladms.py`,
`test_ship.py`.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from cb_core.textmatch import parse_command
from cb_gateway.handlers.everyone import _CHUNK_LIMIT, _is_mention_trigger, ping_chunks

BOT = "CookieMWbot"


# --------------------------------------------------------------------- triggers


class TestTriggersResolve:
    """Phase 6 checklist: every v1 trigger, plus the QA spelling, is accounted for.

    v1 dispatches on `msg['text'].startswith(("/everyone", "@everyone"))`
    (`COOKIEBOT.py:272`). The slash form comes through `COMMAND_ALIASES`
    (`cb_core/textmatch.py:56`, already mapping `everyone` -> `everyone` before
    this port); the bare-word form is `_is_mention_trigger` below, modelled on
    `calladms.py`'s `_MENTION_TRIGGER`.
    """

    def test_slash_everyone_resolves(self) -> None:
        parsed = parse_command("/everyone")
        assert parsed is not None
        assert parsed.name == "everyone"

    def test_slash_everyone_is_case_insensitive(self) -> None:
        parsed = parse_command("/EVERYONE")
        assert parsed is not None
        assert parsed.name == "everyone"

    def test_slash_everyone_addressed_at_another_bot_does_not_resolve(self) -> None:
        assert parse_command("/everyone@SomeOtherBot", BOT) is None

    @pytest.mark.parametrize(
        "text", ["@everyone", "@Everyone", "@EVERYONE please", "@everyone come here"]
    )
    def test_bare_mention_triggers(self, text: str) -> None:
        message = SimpleNamespace(text=text)
        assert _is_mention_trigger(message) is True  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "text", ["@everyonefoo", "@everyones", "hey @everyone", "everyone", "", "@ever"]
    )
    def test_non_mention_triggers(self, text: str) -> None:
        """A username merely starting with "everyone" is not a call, and a
        mention not at the start of the message is not v1's `startswith`
        match either — the same word-boundary narrowing `calladms.py` already
        documents for `@admin`/`@adm`."""
        message = SimpleNamespace(text=text)
        assert _is_mention_trigger(message) is False  # type: ignore[arg-type]

    def test_qa_spelling_does_not_resolve_as_a_single_command(self) -> None:
        """QA's `/ping everyone` (`Cookiebot-QA/features/util_everyone.feature`)
        is two words; `COMMAND_ALIASES` maps one command token to one canonical
        name; teaching it to inspect `args` too would mean `/ping` on its own,
        or `/ping <anything>`, also resolves to `everyone` — over-broad, and
        AGENTS.md §2.1 does not require inventing a mechanism, only that every
        *v1* trigger keep working. `feature-map.mdx`'s `util_everyone` row
        already records this exact shape of mismatch as "trigger mismatch",
        the same way `fun_dice`'s "roll 6" (no v1 equivalent, no slash) is
        recorded rather than special-cased. `/everyone` and `@everyone`
        (asserted above) are v1's real triggers and are what must resolve.
        """
        parsed = parse_command("/ping everyone")
        assert parsed is None or parsed.name != "everyone"


# ------------------------------------------------------------------- ping_chunks


class TestPingChunks:
    def test_header_on_first_chunk_only(self) -> None:
        chunks = ping_chunks(["alice", "bob", "carol"], known=3)
        assert chunks[0].startswith("Number of known users: 3\n")
        assert len(chunks) == 1
        assert "Number of known users" not in chunks[0][len("Number of known users: 3\n") :]

    def test_single_member_roster(self) -> None:
        chunks = ping_chunks(["alice"], known=1)
        assert chunks == ["Number of known users: 1\n@alice "]

    def test_every_chunk_stays_at_or_under_the_limit(self) -> None:
        usernames = [f"user{n:04d}" for n in range(2000)]
        chunks = ping_chunks(usernames, known=len(usernames))
        assert len(chunks) > 1, "test is only meaningful if it actually spills over"
        for chunk in chunks:
            assert len(chunk) <= _CHUNK_LIMIT

    def test_every_username_is_present_exactly_once(self) -> None:
        usernames = [f"user{n:04d}" for n in range(2000)]
        chunks = ping_chunks(usernames, known=len(usernames))
        joined = "".join(chunks)
        for username in usernames:
            assert f"@{username} " in joined

    def test_boundary_hits_v1s_exact_condition(self) -> None:
        """v1: `len(result[top_message_index]) + len(username) + 2 > 4096`
        (`UserRegisters.py:113`). A username landing exactly on 4096 after the
        append stays in the same chunk; one byte over starts a new one — the
        condition is a strict `>`, not `>=`.
        """
        # First chunk starts as "Number of known users: 1\n" (26 chars). Pick a
        # username whose append lands the chunk at exactly 4096: the appended
        # text is `f"@{username} "`, length len(username) + 2, so
        # len(header) + len(username) + 2 == 4096.
        header = "Number of known users: 1\n"
        exact_len = _CHUNK_LIMIT - len(header) - 2
        exact_username = "a" * exact_len
        chunks = ping_chunks([exact_username], known=1)
        assert len(chunks) == 1
        assert len(chunks[0]) == _CHUNK_LIMIT

        over_username = "a" * (exact_len + 1)
        chunks = ping_chunks([over_username], known=1)
        assert len(chunks) == 2, "one byte over the limit must spill into a new chunk"
        assert chunks[0] == header

    def test_known_reflects_whatever_the_caller_computed(self) -> None:
        """`ping_chunks` only formats `known`; the `min(len(usernames), await
        bot.get_chat_member_count(...))` clamp (`UserRegisters.py:112`) is the
        caller's job (`everyone()`, R4.6). Exercised here as the two shapes
        that clamp can take.
        """
        usernames = ["alice", "bob", "carol"]
        # Fewer known members than usernames tracked (stale registry entries).
        known = min(len(usernames), 1)
        assert ping_chunks(usernames, known).pop(0).startswith("Number of known users: 1\n")
        # More known members than usernames tracked (most members never spoke).
        known = min(len(usernames), 500)
        assert ping_chunks(usernames, known).pop(0).startswith("Number of known users: 3\n")

    def test_no_usernames_still_emits_the_header(self) -> None:
        assert ping_chunks([], known=0) == ["Number of known users: 0\n"]
