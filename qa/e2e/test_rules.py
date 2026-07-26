"""`/rules` and `/newrules` — the two-step "reply to set" flow — over real HTTP.

v1's real shape (`cb_gateway/handlers/rules.py`'s own docstring, ported from
`Configurations.py:269-283`): `/newrules` always replies with a fixed prompt,
*regardless of who ran it*; only the reply that follows is admin-checked.
Reproduced here exactly as three round trips: an admin's reply is accepted, a
non-admin's reply is rejected (and the prompt survives), then the admin's
successful reply is asserted end to end.

Every scenario here runs once per language (`qa/e2e/conftest.py`'s `lang`
fixture). `/rules` and its "no rules yet" notice are genuinely localised
(`locales.get("no_rules", lang)`, `locales.get("questions", lang)`), so those
assertions are language-aware below. `/newrules`'s prompt and its two outcome
texts are a different case: `rules.py:50-56` hardcodes them in English and
says so explicitly — "Hardcoded verbatim in v1 (Configurations.py:283) — never
localised" and "Also hardcoded English-only in v1 (Configurations.py:271,278)
— same quirk." v1 itself never translated these, in a Portuguese group or
otherwise, so a `pt` group seeing English here is not a locale bug — it is
byte-for-byte v1 parity. The constants are asserted as English literals in
both language runs on purpose, with this paragraph as the reason, rather than
silently drifting into "why does pt get English" if someone reads only the
assertions.
"""

from __future__ import annotations

import pytest

from cb_core import locales
from qa.e2e.client import SandboxClient, calls_to, describe_recent_calls, messages_in, wait_for

pytestmark = pytest.mark.e2e

# Deliberately English in every language — see the module docstring.
NEW_RULES_PROMPT = (
    "If you are an admin, REPLY THIS MESSAGE with the message that will be "
    "displayed when someone asks for the rules"
)
NOT_ADMIN_TEXT = "You are not a group admin!"
RULES_UPDATED_TEXT = "Updated rules message! ✅"

#: The trigger a speaker of each language actually types
#: (`COMMAND_ALIASES`: `regras` -> `rules`, `novasregras` -> `newrules`).
_RULES_COMMAND = {"en": "/rules", "pt": "/regras"}
_NEWRULES_COMMAND = {"en": "/newrules", "pt": "/novasregras"}


def test_rules_with_none_configured_answers_no_rules(
    sandbox: SandboxClient, group_id: int, lang: str
) -> None:
    member = sandbox.create_user("Remy", "remy")["id"]
    sandbox.join(group_id, member)

    since = len(sandbox.state()["api_calls"])
    sandbox.send_message(group_id, member, text=_RULES_COMMAND[lang])

    expected = locales.get("no_rules", lang)
    wait_for(
        lambda: next(
            (
                c
                for c in calls_to(sandbox.state(), "sendMessage", since)
                if c["payload"].get("text") == expected
            ),
            None,
        ),
        timeout=15.0,
        description=f"answer {_RULES_COMMAND[lang]} with the localised no-rules notice",
        on_timeout=lambda: describe_recent_calls(sandbox.state()),
    )


def test_newrules_round_trip_admin_sets_then_rules_displays_it(
    sandbox: SandboxClient, group_id: int, lang: str
) -> None:
    admin = sandbox.create_user("Adamina", "adamina")["id"]
    sandbox.join(group_id, admin)
    sandbox.patch_member(group_id, admin, role="administrator")

    # Step 1: /newrules always answers with the fixed, English-only prompt,
    # admin or not, in either language group — see the module docstring.
    since = len(sandbox.state()["api_calls"])
    sandbox.send_message(group_id, admin, text=_NEWRULES_COMMAND[lang])
    wait_for(
        lambda: next(
            (
                c
                for c in calls_to(sandbox.state(), "sendMessage", since)
                if c["payload"].get("text") == NEW_RULES_PROMPT
            ),
            None,
        ),
        timeout=15.0,
        description=f"send the {_NEWRULES_COMMAND[lang]} prompt",
        on_timeout=lambda: describe_recent_calls(sandbox.state()),
    )
    prompt_message = next(
        m for m in messages_in(sandbox.state(), group_id) if m["text"] == NEW_RULES_PROMPT
    )

    # Step 2: an admin's reply is accepted, saved, and the prompt is cleaned up.
    since = len(sandbox.state()["api_calls"])
    new_body = "Be nice, no spam. Have fun!"
    sandbox.send_message(
        group_id, admin, text=new_body, reply_to_message_id=prompt_message["message_id"]
    )
    wait_for(
        lambda: next(
            (
                c
                for c in calls_to(sandbox.state(), "sendMessage", since)
                if c["payload"].get("text") == RULES_UPDATED_TEXT
            ),
            None,
        ),
        timeout=15.0,
        description="confirm the new rules were saved",
        on_timeout=lambda: describe_recent_calls(sandbox.state()),
    )
    wait_for(
        lambda: next(
            (
                c
                for c in calls_to(sandbox.state(), "deleteMessage", since)
                if int(c["payload"].get("message_id", -1)) == prompt_message["message_id"]
            ),
            None,
        ),
        timeout=15.0,
        description="delete the answered /newrules prompt",
        on_timeout=lambda: describe_recent_calls(sandbox.state()),
    )

    # Step 3: /rules now shows the saved body plus the localised "questions"
    # tagline (GroupShield.py:60-62 — appended because the body doesn't end in
    # "@MekhyW").
    since = len(sandbox.state()["api_calls"])
    sandbox.send_message(group_id, admin, text=_RULES_COMMAND[lang])
    wait_for(
        lambda: next(
            (
                c
                for c in calls_to(sandbox.state(), "sendMessage", since)
                if new_body in c["payload"].get("text", "")
                and locales.get("questions", lang) in c["payload"].get("text", "")
            ),
            None,
        ),
        timeout=15.0,
        description="display the saved rules with the localised questions tagline",
        on_timeout=lambda: describe_recent_calls(sandbox.state()),
    )


def test_newrules_reply_from_a_non_admin_is_rejected(
    sandbox: SandboxClient, group_id: int, lang: str
) -> None:
    admin = sandbox.create_user("Adamina2", "adamina2")["id"]
    sandbox.join(group_id, admin)
    sandbox.patch_member(group_id, admin, role="administrator")
    outsider = sandbox.create_user("Owen", "owen")["id"]
    sandbox.join(group_id, outsider)

    since = len(sandbox.state()["api_calls"])
    sandbox.send_message(group_id, admin, text=_NEWRULES_COMMAND[lang])
    wait_for(
        lambda: next(
            (
                c
                for c in calls_to(sandbox.state(), "sendMessage", since)
                if c["payload"].get("text") == NEW_RULES_PROMPT
            ),
            None,
        ),
        timeout=15.0,
        description=f"send the {_NEWRULES_COMMAND[lang]} prompt",
        on_timeout=lambda: describe_recent_calls(sandbox.state()),
    )
    prompt_message = next(
        m for m in messages_in(sandbox.state(), group_id) if m["text"] == NEW_RULES_PROMPT
    )

    since = len(sandbox.state()["api_calls"])
    sandbox.send_message(
        group_id,
        outsider,
        text="No rules, do whatever you want.",
        reply_to_message_id=prompt_message["message_id"],
    )
    wait_for(
        lambda: next(
            (
                c
                for c in calls_to(sandbox.state(), "sendMessage", since)
                if c["payload"].get("text") == NOT_ADMIN_TEXT
            ),
            None,
        ),
        timeout=15.0,
        description="reject the non-admin's reply (English-only in every language — see module docstring)",
        on_timeout=lambda: describe_recent_calls(sandbox.state()),
    )
    # The rejection is the *only* thing that happened — the prompt is not
    # deleted and the confirmation text never appears.
    assert not calls_to(sandbox.state(), "deleteMessage", since)
    assert not any(
        c["payload"].get("text") == RULES_UPDATED_TEXT
        for c in calls_to(sandbox.state(), "sendMessage", since)
    )
