"""`/newwelcome` — the two-step "reply to set" flow, and the join it greets.

There was no end-to-end coverage of this command at all, and it was broken in
UAT for every caller, in every group: the prompt contains the literal `<user>`
(it is telling the admin which placeholder to type), the bot sends with
`parse_mode=HTML`, and Telegram rejects the whole call with

    Bad Request: can't parse entities: Unsupported start tag "user" at byte
    offset 127

so `/newwelcome` never answered at all. v1 sent the same string with no
parse_mode (`Configurations.py:267`), so it never had to survive an entity
parser — which is exactly the kind of difference a unit test with a mocked
`bot.send_message` cannot see, because nothing in it parses entities. Only a
real Bot API surface rejects this, which is what the sandbox is.

The prompt and both outcome strings are deliberately English in every language
— `welcome.py:62-90` says so, ported from v1's own quirk — so they are asserted
as English literals in both language runs, the same way `test_rules.py` does.
"""

from __future__ import annotations

from typing import Any

import pytest

from qa.e2e.client import (
    SandboxClient,
    calls_to,
    describe_recent_calls,
    messages_in,
    wait_for,
)

pytestmark = pytest.mark.e2e

# `welcome.py:WELCOME_PROMPT`, as the admin sees it — Telegram renders the
# escaped form back to this, which is also what `_is_welcome_reply` matches on.
WELCOME_PROMPT = (
    "If you are an admin, REPLY THIS MESSAGE with the message that will be "
    "displayed when someone joins the group.\n\n"
    "You can include <user> to be replaced with the user name"
)
WELCOME_UPDATED_TEXT = "Welcome message updated! ✅"
NOT_ADMIN_TEXT = "You are not a group admin!"

_NEWWELCOME_COMMAND = {"en": "/newwelcome", "pt": "/novobemvindo"}


def test_newwelcome_answers_with_the_prompt(
    sandbox: SandboxClient, group_id: int, lang: str
) -> None:
    """The regression. Before the fix this command sent nothing at all: the one
    `sendMessage` it makes was rejected by Telegram's entity parser and the
    handler raised."""
    admin = sandbox.create_user("Willa", "willa")["id"]
    sandbox.join(group_id, admin)
    sandbox.patch_member(group_id, admin, role="administrator")

    since = len(sandbox.state()["api_calls"])
    sandbox.send_message(group_id, admin, text=_NEWWELCOME_COMMAND[lang])

    prompt = wait_for(
        lambda: next(
            (
                message
                for message in messages_in(sandbox.state(), group_id)
                if message["text"] == WELCOME_PROMPT
            ),
            None,
        ),
        timeout=15.0,
        description=f"answer {_NEWWELCOME_COMMAND[lang]} with the prompt",
        on_timeout=lambda: describe_recent_calls(sandbox.state()),
    )
    # The placeholder has to reach the admin as text they can copy. Escaped on
    # the wire, rendered back to `<user>` here — if it arrived as `&lt;user&gt;`
    # the instruction would be telling them to type something that does not work.
    assert "<user>" in prompt["text"]
    assert "&lt;" not in prompt["text"]
    assert not [
        c
        for c in calls_to(sandbox.state(), "sendMessage", since)
        if "Something went wrong" in c["payload"].get("text", "")
    ], "the command failed and the middleware had to apologise for it"


def test_newwelcome_round_trip_admin_sets_it_then_a_joiner_sees_it(
    sandbox: SandboxClient, group_id: int, pg_conn: Any, lang: str
) -> None:
    admin = sandbox.create_user("Wendell", "wendell")["id"]
    sandbox.join(group_id, admin)
    sandbox.patch_member(group_id, admin, role="administrator")

    sandbox.send_message(group_id, admin, text=_NEWWELCOME_COMMAND[lang])
    prompt = wait_for(
        lambda: next(
            (
                message
                for message in messages_in(sandbox.state(), group_id)
                if message["text"] == WELCOME_PROMPT
            ),
            None,
        ),
        timeout=15.0,
        description="answer with the prompt",
        on_timeout=lambda: describe_recent_calls(sandbox.state()),
    )

    since = len(sandbox.state()["api_calls"])
    sandbox.send_message(
        group_id,
        admin,
        text="Welcome <user>, make yourself at home!",
        reply_to_message_id=int(prompt["message_id"]),
    )
    wait_for(
        lambda: next(
            (
                c
                for c in calls_to(sandbox.state(), "sendMessage", since)
                if WELCOME_UPDATED_TEXT in c["payload"].get("text", "")
            ),
            None,
        ),
        timeout=15.0,
        description="confirm the new welcome was saved",
        on_timeout=lambda: describe_recent_calls(sandbox.state()),
    )

    # The template is stored as written. Substitution belongs to the send, not
    # to the write: a body with the joiner's name baked in would greet everyone
    # who ever joins by the name of whoever happened to join first.
    stored = pg_conn.execute(
        "SELECT body FROM group_welcomes WHERE group_id = %s", (group_id,)
    ).fetchone()
    assert stored is not None and stored[0] == "Welcome <user>, make yourself at home!"

    # And the half that proves the stored text is actually used: `<user>` is one
    # of the ten placeholders `_substitute_user_tags` expands, so the newcomer's
    # own handle must appear in the greeting rather than the literal tag.
    #
    # Matched on the newcomer's handle, not merely on the body: the harness
    # delivers queued join updates in bursts, so an earlier join in this same
    # group (the admin's own, above) can be greeted *after* the save and renders
    # the same body under a different name. Waiting for "a greeting with this
    # body" would then pass or fail on delivery order.
    since = len(sandbox.state()["api_calls"])
    newcomer = sandbox.create_user("Nadia", "nadia_joins")["id"]
    sandbox.join(group_id, newcomer)
    greeting = wait_for(
        lambda: next(
            (
                c
                for c in calls_to(sandbox.state(), "sendMessage", since)
                if "@nadia_joins" in c["payload"].get("text", "")
            ),
            None,
        ),
        timeout=15.0,
        description="greet the newcomer by name with the saved welcome",
        on_timeout=lambda: describe_recent_calls(sandbox.state()),
    )
    assert "make yourself at home" in greeting["payload"]["text"]
    assert "<user>" not in greeting["payload"]["text"]


def test_a_non_admins_reply_is_rejected_and_the_prompt_survives(
    sandbox: SandboxClient, group_id: int, lang: str
) -> None:
    """v1 gates the reply, not the command (`welcome.py`'s docstring). The
    prompt staying is what lets the admin answer it afterwards."""
    member = sandbox.create_user("Mallory", "mallory")["id"]
    sandbox.join(group_id, member)

    sandbox.send_message(group_id, member, text=_NEWWELCOME_COMMAND[lang])
    prompt = wait_for(
        lambda: next(
            (
                message
                for message in messages_in(sandbox.state(), group_id)
                if message["text"] == WELCOME_PROMPT
            ),
            None,
        ),
        timeout=15.0,
        description="answer a non-admin with the prompt too",
        on_timeout=lambda: describe_recent_calls(sandbox.state()),
    )

    since = len(sandbox.state()["api_calls"])
    sandbox.send_message(
        group_id, member, text="I run this group now", reply_to_message_id=int(prompt["message_id"])
    )
    wait_for(
        lambda: next(
            (
                c
                for c in calls_to(sandbox.state(), "sendMessage", since)
                if NOT_ADMIN_TEXT in c["payload"].get("text", "")
            ),
            None,
        ),
        timeout=15.0,
        description="reject the non-admin's reply",
        on_timeout=lambda: describe_recent_calls(sandbox.state()),
    )
    assert not [
        c
        for c in calls_to(sandbox.state(), "deleteMessage", since)
        if int(c["payload"].get("message_id", -1)) == int(prompt["message_id"])
    ], "the prompt was deleted on a rejected reply, so the admin has nothing left to answer"
