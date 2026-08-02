"""Unit coverage for fun_complaint — pure logic only, no dispatcher, no Telegram.

See `.specs/features/fun_complaint/spec.md` and `design.md` for the full v1
behaviour contract; `docs/contracts/fun_complaint.md` will carry the same once
T6 lands. `qa/features/fun_complaint.feature` + `qa/test_fun_complaint.py` are
the end-to-end version of the same assertions.

T3 (the handler) has not landed yet. The alias tests below need only
`cb_gateway.filters.CommandName`, which already exists, so they run and pass.
Everything else imports `cb_gateway.handlers.complaint` lazily, inside each
test body, so a missing handler module fails those tests at call time with an
ImportError/AttributeError rather than killing collection for the whole file.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from typing import Any

import pytest

from cb_gateway.filters import CommandName

_PROTOCOL_RE = re.compile(r"^\d{2}-\d{6}/\d{4}$")


@dataclass
class _FakeUser:
    id: int
    username: str | None = None
    first_name: str | None = None


@dataclass
class _FakeMessage:
    """Only the attributes the handler / filters actually read.

    `caption` is separate from `text`: entry 2 matches on a *photo's* caption
    (`COOKIEBOT.py:300-301`), never on `.text`, so the two must be
    independently settable to exercise D-CP-3's caption-vs-text distinction.
    """

    text: str | None = None
    caption: str | None = None
    reply_to_message: Any = None
    from_user: _FakeUser | None = None
    chat: Any = field(default_factory=lambda: type("Chat", (), {"id": -100})())
    bot: Any = None
    message_id: int = 0

    async def reply(self, text: str) -> None:  # pragma: no cover - overridden per test
        raise NotImplementedError


# ------------------------------------------------------------- trigger surface — entry 1


@pytest.mark.parametrize(
    "text",
    ["/milton", "/reclamacao", "/reclamação", "/complaint", "/queja"],
)
@pytest.mark.asyncio
async def test_every_v1_complaint_alias_resolves_bare(text: str) -> None:
    result = await CommandName("complaint")(_FakeMessage(text), bot_username="CookieMWbot")
    assert result is not False, f"{text!r} did not resolve to the complaint command"


@pytest.mark.parametrize(
    "text",
    [
        "/milton please help",
        "/reclamacao my order never arrived",
        "/reclamação meu pedido nunca chegou",
        "/complaint the soup is cold",
        "/queja el pedido nunca llego",
    ],
)
@pytest.mark.asyncio
async def test_every_v1_complaint_alias_resolves_with_argument(text: str) -> None:
    result = await CommandName("complaint")(_FakeMessage(text), bot_username="CookieMWbot")
    assert result is not False, f"{text!r} did not resolve to the complaint command"


@pytest.mark.parametrize(
    "text",
    [
        "/milton@CookieMWbot",
        "/reclamacao@CookieMWbot",
        "/reclamação@CookieMWbot",
        "/complaint@CookieMWbot",
        "/queja@CookieMWbot",
    ],
)
@pytest.mark.asyncio
async def test_every_v1_complaint_alias_resolves_with_botname(text: str) -> None:
    result = await CommandName("complaint")(_FakeMessage(text), bot_username="CookieMWbot")
    assert result is not False, f"{text!r} did not resolve to the complaint command"


@pytest.mark.asyncio
async def test_complaint_addressed_at_a_different_bot_does_not_resolve() -> None:
    result = await CommandName("complaint")(
        _FakeMessage("/complaint@SomeOtherBot"), bot_username="CookieMWbot"
    )
    assert result is False


@pytest.mark.asyncio
async def test_unrelated_command_does_not_resolve_as_complaint() -> None:
    result = await CommandName("complaint")(_FakeMessage("/isalive"), bot_username="CookieMWbot")
    assert result is False


# ------------------------------------------------------------- reply capture — entry 2


class TestIsMiltonReply:
    """`_is_milton_reply` — design R2.4: substring containment over both
    `MILTON_SIGNATURES`, checked against the replied-to message's *caption*
    (a photo caption, not `.text`), and only reachable when the incoming
    message does not itself look like a command. Modelled on
    `rules._is_new_rules_reply` / `welcome._is_welcome_reply`."""

    def test_no_reply_does_not_match(self) -> None:
        from cb_gateway.handlers import complaint as complaint_handler

        assert not complaint_handler._is_milton_reply(_FakeMessage("some text"))  # noqa: SLF001

    def test_caption_containing_english_signature_matches(self) -> None:
        from cb_gateway.handlers import complaint as complaint_handler

        reply = _FakeMessage(caption="Hey there, I'm Milton from HR. How can I help?")
        message = _FakeMessage("my complaint text", reply_to_message=reply)
        assert complaint_handler._is_milton_reply(message)  # noqa: SLF001

    def test_caption_containing_portuguese_signature_matches(self) -> None:
        from cb_gateway.handlers import complaint as complaint_handler

        reply = _FakeMessage(caption="Oi, eu sou o Milton do RH. Como posso ajudar?")
        message = _FakeMessage("minha reclamação", reply_to_message=reply)
        assert complaint_handler._is_milton_reply(message)  # noqa: SLF001

    def test_signature_embedded_mid_caption_matches(self) -> None:
        """Substring containment, not equality or anchoring (D-CP-3): the
        signature may sit anywhere inside a longer caption."""
        from cb_gateway.handlers import complaint as complaint_handler

        reply = _FakeMessage(caption="preamble text Milton from HR. trailing text")
        message = _FakeMessage("text", reply_to_message=reply)
        assert complaint_handler._is_milton_reply(message)  # noqa: SLF001

    def test_caption_with_neither_signature_does_not_match(self) -> None:
        from cb_gateway.handlers import complaint as complaint_handler

        reply = _FakeMessage(caption="Just a regular photo caption.")
        message = _FakeMessage("text", reply_to_message=reply)
        assert not complaint_handler._is_milton_reply(message)  # noqa: SLF001

    def test_reply_to_message_with_text_but_no_caption_does_not_match(self) -> None:
        """v1 matches on `msg['reply_to_message']['caption']`
        (`COOKIEBOT.py:300-301`) — a text message, even one containing the
        signature in its `.text`, is not a photo and does not arm entry 2."""
        from cb_gateway.handlers import complaint as complaint_handler

        reply = _FakeMessage(text="Milton from HR. is typed here, not captioned")
        message = _FakeMessage("text", reply_to_message=reply)
        assert not complaint_handler._is_milton_reply(message)  # noqa: SLF001

    def test_message_that_is_itself_a_command_does_not_match(self) -> None:
        """v1's whole command-dispatch chain lives inside `if text.startswith("/")
        ...`, and the reply-capture branch is a sibling `elif`
        (`COOKIEBOT.py:186,300-301`) — reachable only when the incoming text
        does not itself look like a command."""
        from cb_gateway.handlers import complaint as complaint_handler

        reply = _FakeMessage(caption="Milton from HR. wants your complaint")
        message = _FakeMessage("/complaint", reply_to_message=reply)
        assert not complaint_handler._is_milton_reply(message)  # noqa: SLF001


# ------------------------------------------------------------------- protocol number


def test_protocol_matches_v1_shape_over_a_seeded_rng() -> None:
    """v1: `f"{randint(10,99)}-{randint(100000,999999)}/{now().year}"`
    (`Miscellaneous.py:253`)."""
    from cb_gateway.handlers import complaint as complaint_handler

    rng = random.Random(1234)
    protocol = complaint_handler._build_protocol(rng)  # noqa: SLF001
    assert _PROTOCOL_RE.match(protocol), protocol


def test_protocol_is_reproducible_for_the_same_seed() -> None:
    from cb_gateway.handlers import complaint as complaint_handler

    first = complaint_handler._build_protocol(random.Random(42))  # noqa: SLF001
    second = complaint_handler._build_protocol(random.Random(42))  # noqa: SLF001
    assert first == second


# ------------------------------------------------------------------- photo choice


@pytest.mark.parametrize(
    ("lang", "expected"),
    [
        ("pt", "milton_pt.jpg"),
        ("en", "milton_eng.jpg"),
        ("es", "milton_eng.jpg"),
    ],
)
def test_photo_choice_is_pt_or_fallback_to_eng(lang: str, expected: str) -> None:
    """D-CP-2: `"pt" if lang == "pt" else "eng"` — an equality check against
    the resolved language, not a locale lookup, so every non-`pt` language
    (including Spanish, which has no `milton_es.jpg`) falls through to the
    English photo."""
    from cb_gateway.handlers import complaint as complaint_handler

    assert complaint_handler._photo_filename(lang) == expected  # noqa: SLF001
