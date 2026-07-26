"""`/privacy` and `/commands` round trips, driven over real HTTP.

The simplest possible scenario in this suite on purpose: neither command needs
an admin, a database write, or any prior group configuration, so a failure
here can only be about the plumbing (sandbox <-> gateway over HTTP, the
control API, the poll loop) — see `docs/E2E.md`. Everything after this file
builds on the same pattern with progressively more state.

Both tests run once per language in `qa/e2e/conftest.py`'s `lang` fixture
(`group_id` is parametrized on it): the command each test sends is the trigger
a speaker of that group's language would actually type
(`cb_core.textmatch.COMMAND_ALIASES`: `privacidade` -> `privacy`, `comandos` ->
`commands`), and the expected reply is read back from the same catalog the
handler itself renders from (`cb_gateway/handlers/privacy.py`'s `t(ctx,
"privacy")`, `listcommand.py`'s `locales.text("Cookiebot_functions", ctx.lang)`)
rather than pinned to `"en"`.
"""

from __future__ import annotations

import pytest

from cb_core import locales
from qa.e2e.client import SandboxClient, calls_to, describe_recent_calls, messages_in, wait_for

pytestmark = pytest.mark.e2e

_PRIVACY_URL = "https://cookiebotfur.net/privacy"

#: The trigger a speaker of each language actually types. `/privacidade` is a
#: real v1 alias (`COMMAND_ALIASES["privacidade"] = "privacy"`), not a guess.
_PRIVACY_COMMAND = {"en": "/privacy", "pt": "/privacidade"}
_COMMANDS_COMMAND = {"en": "/commands", "pt": "/comandos"}


def test_privacy_replies_with_the_privacy_url(
    sandbox: SandboxClient, group_id: int, lang: str
) -> None:
    member = sandbox.create_user("Priya", "priya")["id"]
    sandbox.join(group_id, member)

    state = sandbox.state()
    since = len(state["api_calls"])

    sandbox.send_message(group_id, member, text=_PRIVACY_COMMAND[lang])

    wait_for(
        lambda: next(
            (
                c
                for c in calls_to(sandbox.state(), "sendMessage", since)
                if _PRIVACY_URL in c["payload"].get("text", "")
            ),
            None,
        ),
        timeout=10.0,
        description=f"reply to {_PRIVACY_COMMAND[lang]} with {_PRIVACY_URL!r}",
        on_timeout=lambda: describe_recent_calls(sandbox.state()),
    )

    # The reply must land in the chat transcript too, not only in the raw
    # api_calls log — GET /api/state's `messages` is what a human (or the web
    # client) actually reads. The URL is embedded in a different sentence per
    # language (`locales.get("privacy", lang)`), but the URL itself is the one
    # constant a test can check without retyping either sentence.
    final = sandbox.state()
    bot_messages = [m for m in messages_in(final, group_id, since=0) if m["from_id"] != member]
    assert any(_PRIVACY_URL in (m["text"] or "") for m in bot_messages), final["messages"]
    # And the full sentence really is the localised one, not just a URL that
    # happens to appear in whatever text came back.
    expected_sentence = locales.get("privacy", lang)
    assert any(m["text"] == expected_sentence for m in bot_messages), final["messages"]


def test_commands_lists_the_bots_features(sandbox: SandboxClient, group_id: int, lang: str) -> None:
    member = sandbox.create_user("Cole", "cole")["id"]
    sandbox.join(group_id, member)

    since = len(sandbox.state()["api_calls"])
    sandbox.send_message(group_id, member, text=_COMMANDS_COMMAND[lang])

    # cb_core.locales.text ships the exact catalog file the handler sends
    # verbatim (packages/cb-core/src/cb_core/locale_data/<lang>/Cookiebot_functions.txt)
    # — asserting against it, not a retyped substring, is what proves the real,
    # language-specific help text reached the group rather than some other reply.
    expected = locales.text("Cookiebot_functions", lang)
    wait_for(
        lambda: next(
            (
                c
                for c in calls_to(sandbox.state(), "sendMessage", since)
                if c["payload"].get("text") == expected
            ),
            None,
        ),
        timeout=10.0,
        description=f"reply to {_COMMANDS_COMMAND[lang]} with the localised Cookiebot_functions catalog text",
        on_timeout=lambda: describe_recent_calls(sandbox.state()),
    )
