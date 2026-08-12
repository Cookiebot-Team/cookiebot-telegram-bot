"""Unit coverage for fun_death — pure logic, plus the empty-pool degrade.

See `.specs/features/fun_death/spec.md` for the full v1 behaviour contract and
`docs/contracts/fun_death.md` for the same, ported. `qa/features/fun_death.feature`
+ `qa/test_fun_death.py` are the end-to-end version of these assertions, driven
through the real dispatcher against a seeded fake pool.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

import pytest

from cb_core import legacy_assets, locales
from cb_core.legacy_assets import LegacyAsset
from cb_core.textmatch import ParsedCommand, parse_command
from cb_gateway.filters import CommandName
from cb_gateway.handlers import death as death_module
from cb_gateway.handlers.death import is_gif, render_caption, resolve_target

BOT = "CookieMWbot"


def _parsed(text: str) -> ParsedCommand:
    parsed = parse_command(text, BOT)
    assert parsed is not None, f"{text!r} did not parse as a command at all"
    return parsed


@dataclass
class _FakeMessage:
    """Only the attributes the handler / filters actually read."""

    text: str | None = None


# --------------------------------------------------------------------- triggers


@pytest.mark.parametrize("text", ["/death", "/morte", "/muerte", "/DEATH", "/death @someone"])
@pytest.mark.asyncio
async def test_every_v1_trigger_resolves(text: str) -> None:
    """`/death`, `/morte`, `/muerte` all map to the `death` canonical name
    (`cb_core/textmatch.py:COMMAND_ALIASES`, already shipped, confirmed here
    rather than re-declared)."""
    result = await CommandName("death")(_FakeMessage(text), bot_username=BOT)
    assert result is not False, f"{text!r} did not resolve to the death command"
    assert isinstance(result, dict)
    assert result["parsed"].name == "death"


@pytest.mark.asyncio
async def test_addressed_at_another_bot_does_not_resolve() -> None:
    result = await CommandName("death")(_FakeMessage("/death@SomeOtherBot"), bot_username=BOT)
    assert result is False


# ------------------------------------------------------------- target resolution


def test_branch_one_tagged_token_wins_over_reply_and_sender() -> None:
    """v1: `len(msg['text'].split()) > 1` (`Miscellaneous.py:341-342`) — the
    raw second token, un-resolved, even when a reply and a username are also
    present."""
    target, skip = resolve_target(
        "@stranger", reply_first_name="Replier", sender_username="alice", sender_first_name="Alice"
    )
    assert target == "@stranger"
    assert skip is False


def test_branch_two_reply_first_name_when_no_tag() -> None:
    """v1: `msg['reply_to_message']['from']['first_name']` (`:343-344`) —
    first name, never username."""
    target, skip = resolve_target(
        None, reply_first_name="Bob", sender_username="alice", sender_first_name="Alice"
    )
    assert target == "Bob"
    assert skip is False


def test_branch_three_own_username_with_skull_prefix() -> None:
    """v1: `'💀💀💀 @'+username` when the caller has one (`:345-346`)."""
    target, skip = resolve_target(
        None, reply_first_name=None, sender_username="alice", sender_first_name="Alice"
    )
    assert target == "@alice"
    assert skip is False


def test_branch_three_no_username_drops_the_skull_prefix() -> None:
    """D-DE-1: the operator-precedence bug. No username -> bare first name,
    and `skip_skull_prefix` is the only case that comes back True."""
    target, skip = resolve_target(
        None, reply_first_name=None, sender_username=None, sender_first_name="Alice"
    )
    assert target == "Alice"
    assert skip is True


def test_empty_tagged_token_is_treated_as_absent() -> None:
    """`parsed.args.split()` gives `[]` for a bare `/death`, so
    `tokens[0] if tokens else None` is `None`, not `""` -- confirmed here since
    an empty-string tag would otherwise silently fall into branch ① instead of
    ②/③."""
    target, skip = resolve_target(
        "", reply_first_name="Bob", sender_username="alice", sender_first_name="Alice"
    )
    assert target == "Bob"
    assert skip is False


# ---------------------------------------------------------------------- caption


def test_render_caption_keeps_the_skull_prefix_by_default() -> None:
    text = render_caption("@alice", skip_skull_prefix=False, lang="en")
    assert text.startswith("💀💀💀 @alice")


def test_render_caption_drops_the_prefix_when_asked() -> None:
    """D-DE-1 reproduced through the caption builder: `skip_skull_prefix=True`
    (branch ③, no username) means the caption starts with the bare target."""
    text = render_caption("Alice", skip_skull_prefix=True, lang="en")
    assert text.startswith("Alice")
    assert "💀" not in text.split("Alice", 1)[0]


@pytest.mark.parametrize("lang", ["en", "pt", "es"])
def test_render_caption_resolves_in_every_v1_language(lang: str) -> None:
    """No leftover `%(...)s` placeholder in any of the three shipped
    catalogs — the real regression `locales.get`/`get_nested`'s malformed-
    substitution guard exists to catch."""
    text = render_caption("Alice", skip_skull_prefix=False, lang=lang)
    assert "%(" not in text, f"unsubstituted placeholder in {lang}: {text!r}"


def test_render_caption_draws_from_the_death_reason_pool() -> None:
    random.seed(11)
    text = render_caption("Alice", skip_skull_prefix=False, lang="en")
    pool = locales.lines("death", "en")
    assert any(line in text for line in pool), text


def test_render_caption_uses_a_variant_from_the_catalog() -> None:
    variants = locales.nested_value("death", "variants", "en")
    assert isinstance(variants, list) and variants
    random.seed(3)
    text = render_caption("Alice", skip_skull_prefix=False, lang="en")
    assert any(str(variant) in text for variant in variants), text


def test_render_caption_is_seeded_by_the_injected_rng() -> None:
    """Same shape `fun_ship.render`/`fun_fortune.pick_lucky_numbers` already
    established: an injectable `random.Random` makes the draw reproducible
    without touching the module-global one."""
    text_a = render_caption("Alice", skip_skull_prefix=False, lang="en", rng=random.Random(42))
    text_b = render_caption("Alice", skip_skull_prefix=False, lang="en", rng=random.Random(42))
    assert text_a == text_b


# --------------------------------------------------------------------- gif/photo


@pytest.mark.parametrize(
    ("source_path", "expected"),
    [
        ("Death/skull.gif", True),
        ("Death/skull.GIF", True),
        ("Death/photo.jpg", False),
        ("Death/photo.png", False),
        ("Death/no_extension", False),
    ],
)
def test_is_gif_reads_the_source_filename(source_path: str, expected: bool) -> None:
    assert is_gif(source_path) is expected


# ------------------------------------------------------------------ empty pool


@dataclass
class _FakeUser:
    id: int = 1
    username: str | None = "alice"
    first_name: str = "Alice"


@dataclass
class _FakeChat:
    id: int = -100
    type: str = "supergroup"


@dataclass
class _FakeBot:
    """Only what `_deliver` itself calls directly."""

    sent_chat_actions: list[tuple[int, str]] = field(default_factory=list)

    async def send_chat_action(self, chat_id: int, action: str) -> None:
        self.sent_chat_actions.append((chat_id, action))


@dataclass
class _FakeReplyMessage:
    text: str = "/death"
    reply_to_message: Any = None
    from_user: _FakeUser | None = field(default_factory=_FakeUser)
    chat: _FakeChat = field(default_factory=_FakeChat)
    message_id: int = 1
    reacted: bool = False
    replies: list[str] = field(default_factory=list)
    photo_calls: list[str] = field(default_factory=list)
    animation_calls: list[str] = field(default_factory=list)

    async def react(self, **_kwargs: Any) -> None:
        self.reacted = True

    async def reply(self, text: str) -> None:
        self.replies.append(text)

    async def reply_photo(self, _file: Any, caption: str = "") -> None:  # pragma: no cover
        self.photo_calls.append(caption)

    async def reply_animation(self, _file: Any, caption: str = "") -> None:  # pragma: no cover
        self.animation_calls.append(caption)


# `_deliver` is where the empty-pool guarantee (D-DE-3) and the gif/photo
# dispatch actually live; it takes `lang`/`tagged_token` rather than a
# `ChatContext`/`ParsedCommand` precisely so these two behaviours are testable
# without a database (`_deliver`'s own docstring explains the split). Reached
# below as a private member, suppressed per call, the same way
# `test_fun_random.py` reaches `fun_random._should_pool`/`_pool`.


@pytest.mark.asyncio
async def test_empty_pool_sends_nothing_and_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """D-DE-3: `legacy_assets.choose` returning `None` (the "catalog not
    generated yet" state this checkout is actually in — module docstring)
    must degrade to no reply, never propagate v1's `ValueError`."""
    monkeypatch.setattr(legacy_assets, "choose", lambda *_a, **_kw: None)

    message = _FakeReplyMessage()
    bot = _FakeBot()
    await death_module._deliver(message, bot, "en", None)  # noqa: SLF001

    assert message.reacted is True  # module docstring: react fires before the pool check
    assert bot.sent_chat_actions == [(-100, "upload_photo")]
    assert message.replies == []
    assert message.photo_calls == []
    assert message.animation_calls == []


@pytest.mark.asyncio
async def test_populated_pool_sends_a_photo_or_animation(monkeypatch: pytest.MonkeyPatch) -> None:
    entry = LegacyAsset(
        source_path="Death/skull.gif",
        destination_key="legacy/v1-bucket/ab/abcd1234.gif",
        byte_size=12,
        content_hash="abcd1234",
    )
    monkeypatch.setattr(legacy_assets, "choose", lambda *_a, **_kw: entry)

    from cb_core import storage as storage_module

    async def _fake_get(_key: str) -> bytes:
        return b"gif-bytes"

    class _FakeStore:
        get = staticmethod(_fake_get)

    monkeypatch.setattr(storage_module, "store", lambda: _FakeStore())

    message = _FakeReplyMessage()
    bot = _FakeBot()
    await death_module._deliver(message, bot, "en", None)  # noqa: SLF001

    assert len(message.animation_calls) == 1
    assert message.animation_calls[0].startswith("💀💀💀 @alice")
    assert message.photo_calls == []


@pytest.mark.asyncio
async def test_still_image_pool_entry_sends_a_photo(monkeypatch: pytest.MonkeyPatch) -> None:
    entry = LegacyAsset(
        source_path="Death/skull.jpg",
        destination_key="legacy/v1-bucket/cd/cdef5678.jpg",
        byte_size=12,
        content_hash="cdef5678",
    )
    monkeypatch.setattr(legacy_assets, "choose", lambda *_a, **_kw: entry)

    from cb_core import storage as storage_module

    async def _fake_get(_key: str) -> bytes:
        return b"jpg-bytes"

    class _FakeStore:
        get = staticmethod(_fake_get)

    monkeypatch.setattr(storage_module, "store", lambda: _FakeStore())

    message = _FakeReplyMessage()
    bot = _FakeBot()
    await death_module._deliver(message, bot, "en", "@bob")  # noqa: SLF001

    assert len(message.photo_calls) == 1
    assert message.photo_calls[0].startswith("💀💀💀 @bob")
    assert message.animation_calls == []
