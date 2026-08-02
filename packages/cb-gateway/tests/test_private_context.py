"""Unit tests for `cb_gateway.private_context` (design R1, `.specs/features/private_dispatch/`).

No Telegram session, no database — `private_context_for` is synchronous and
reads only fields already on a `Message`, so these are plain object tests.
"""

from __future__ import annotations

from types import SimpleNamespace

from cb_gateway.private_context import PrivateContext, private_context_for


class TestPrivateContextFor:
    def test_reads_the_sender_id(self) -> None:
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=555001), chat=SimpleNamespace(id=555001)
        )
        ctx = private_context_for(message)  # type: ignore[arg-type]
        assert ctx == PrivateContext(user_id=555001)

    def test_falls_back_to_chat_id_when_from_user_is_missing(self) -> None:
        """Never actually reached in production (every call site is behind
        `F.chat.type == ChatType.PRIVATE`, and Telegram always attaches `from`
        there), but the fallback must still be correct: a DM's chat id and
        the sender's user id are the same number by Telegram's own convention.
        """
        message = SimpleNamespace(from_user=None, chat=SimpleNamespace(id=555002))
        ctx = private_context_for(message)  # type: ignore[arg-type]
        assert ctx == PrivateContext(user_id=555002)


class TestPrivateContextShape:
    def test_has_no_group_id_field(self) -> None:
        """The load-bearing design decision (design R1.3): a type that cannot
        hold a `group_id` cannot be handed to `cb_core.group_config` or
        `cb_core.admins` and produce a plausible-looking wrong answer."""
        fields = PrivateContext.__dataclass_fields__
        assert "group_id" not in fields
        assert "lang" not in fields
        assert set(fields) == {"user_id"}

    def test_is_frozen(self) -> None:
        ctx = PrivateContext(user_id=1)
        try:
            ctx.user_id = 2  # type: ignore[misc]
        except AttributeError:
            pass
        else:
            raise AssertionError("PrivateContext must be frozen")
