"""What a user is told when a handler fails, and what a span records about it.

The regression this file exists for: the reply that was supposed to hand the
user a trace id shipped with `{trace}` in the catalog while `locales.get`
substitutes `%(trace)s`, so the bot answered every failure with the literal
text "Reference: {trace}" — a reference nobody could look up, in a message
whose entire purpose was to be looked up. `%`-formatting silently returns the
string unchanged when it finds no conversion specifier, so nothing raised and
nothing logged.
"""

from __future__ import annotations

import pytest

from cb_core import errors, locales
from cb_gateway.telemetry import error_reason_for_chat

TRACE = "08f431777ab7f8f5e7dbb4b570197d70"


def _wrapped_failure() -> errors.CbError:
    try:
        with errors.fail_as("group_config.set_config", group_id=-5528379079, columns="language"):
            raise ValueError(
                'insert or update on table "group_configs_102052" violates foreign key '
                'constraint "group_configs_group_id_fkey_102052"'
            )
    except errors.CbError as exc:
        return exc
    raise AssertionError("fail_as did not raise")  # pragma: no cover


class TestTheMessageCarriesItsTraceId:
    @pytest.mark.parametrize("lang", ["en", "pt", "es"])
    def test_the_trace_id_is_substituted_not_printed(self, lang: str) -> None:
        text = locales.get("handler_error", lang, trace=TRACE, reason="something broke")
        assert TRACE in text
        assert "{trace}" not in text
        assert "%(trace)s" not in text

    @pytest.mark.parametrize("lang", ["en", "pt", "es"])
    def test_the_reason_is_substituted_too(self, lang: str) -> None:
        text = locales.get("handler_error", lang, trace=TRACE, reason="disk on fire")
        assert "disk on fire" in text
        assert "%(reason)s" not in text

    @pytest.mark.parametrize("lang", ["en", "pt", "es"])
    def test_the_config_write_failure_carries_both(self, lang: str) -> None:
        text = locales.get("config_write_failed", lang, trace=TRACE, reason="disk on fire")
        assert TRACE in text
        assert "disk on fire" in text

    def test_the_surrounding_sentence_is_translated(self) -> None:
        """The bot's own words are localised even though the reason is not."""
        en = locales.get("handler_error", "en", trace=TRACE, reason="x")
        pt = locales.get("handler_error", "pt", trace=TRACE, reason="x")
        assert en != pt


class TestTheReason:
    def test_is_the_innermost_failure_not_the_wrapper(self) -> None:
        reason = error_reason_for_chat(_wrapped_failure())
        assert reason.startswith("insert or update on table")
        assert "set_config" not in reason

    def test_is_english_whatever_the_group_speaks(self) -> None:
        """An exception message is English wherever it came from. Translating
        the frame around it and not the payload makes half a sentence; machine
        translating the payload corrupts the identifiers that make it useful."""
        assert error_reason_for_chat(None) == "the bot could not finish the command"

    def test_is_escaped_for_html(self) -> None:
        """Telegram quotes the offending markup back at you in a parse error,
        and the reply that reports it is itself sent with parse_mode=HTML. The
        unescaped form fails to send the message explaining a failure to send a
        message — which is how `/newwelcome` failed twice over."""
        exc = ValueError('can\'t parse entities: Unsupported start tag "user" at byte offset 127')
        reason = error_reason_for_chat(exc)
        assert "<user>" not in reason
        assert "&lt;" not in reason  # no tag in this message, nothing to escape

        markup = ValueError("rejected <user> and <b>bold</b>")
        escaped = error_reason_for_chat(markup)
        assert "&lt;user&gt;" in escaped
        assert "<b>" not in escaped

    def test_falls_back_rather_than_rendering_an_empty_quote(self) -> None:
        assert error_reason_for_chat(TimeoutError()) == "TimeoutError"
