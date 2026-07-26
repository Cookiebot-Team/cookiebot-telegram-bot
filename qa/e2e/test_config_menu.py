"""`/configurar` (aliased `/config`) — permission gating, over real HTTP.

The highest-value case in this file is the anonymous-admin one: v1
(`Configurations.py:141`) checks `str(from_id) not in listaadmins_id`, and an
anonymous admin's `from.id` is Telegram's synthetic `GroupAnonymousBot`, so a
genuine admin posting anonymously was always rejected and shown a tutorial
video about a Telegram feature that was never the problem
(`cb_gateway/handlers/config_menu.py`'s own module docstring). `cb_core.admins
.resolve_actor` fixes this — an anonymous sender is trusted as an admin
unconditionally — and this test proves it the only way an end-to-end suite
can: the denial text and the tutorial video must never appear for the
anonymous admin, only the "admin" plumbing.

Both outcomes of the successful path are covered too, and the difference
between them is the point: a handler answers privately with
`bot.send_message(ctx.actor.user_id, ...)`, so the DM it needs is the chat
whose id *is* that user id. `POST /api/users/{id}/dm` mints exactly that and
stands for the user having pressed Start; without it the bot is refused with
Telegram's own `403 Forbidden: bot can't initiate conversation with a user`
and falls back to "couldn't reach you privately" in the group. Both branches
are real Telegram behaviour, and each is asserted below.

An anonymous admin always takes the fallback branch, DM or no DM — there is no
real user id behind `GroupAnonymousBot` to message.

Every test runs once per language (`qa/e2e/conftest.py`'s `lang` fixture).
This file is the interesting case for that, because `config_menu.py` does
*not* behave the way the rest of this suite's "hardcoded in v1, English in
every language" strings do: its own module docstring says the menu's labels
and per-field prompts stay hardcoded English regardless of group language, but
`_DENIED_TEXT` and `_CANNOT_DM_TEXT` (and `_SENT_DM_TEXT`, unused by any test
here) are each a hand-translated `{"pt": ..., "en": ..., "es": ...}` dict,
picked by `_text_for(table, ctx.lang)` — a real, working per-group
translation, not a v1-parity English constant like `rules.py`'s
`NOT_ADMIN_TEXT`. So unlike `test_rules.py`, the two user-facing strings this
file checks are asserted in the *group's own language* below, not pinned to
English — that is the correct behaviour to assert, and pinning both languages
to the English literal would have been the bug.
"""

from __future__ import annotations

import pytest

from qa.e2e.client import (
    SandboxClient,
    calls_to,
    describe_recent_calls,
    messages_in,
    wait_for,
)

pytestmark = pytest.mark.e2e

# `cb_gateway/handlers/config_menu.py`'s `_DENIED_TEXT` / `_CANNOT_DM_TEXT` —
# hand-translated per language (not `cb_core.locales`, see the module
# docstring there), reproduced here as a golden value the same way
# `test_rules.py` reproduces its own handler constants, so a change to either
# dict is a visible test diff rather than a silently-passing substring match.
_DENIED_TEXT = {
    "en": (
        "You don't have permission to use this command or are in anonymous mode\n"
        "<blockquote> Are you speaking as a user and not as a channel? The 'remain anonymous' "
        "permission should be turned off! </blockquote>"
    ),
    "pt": (
        "Você não tem permissão para configurar o bot, ou está anônimo!\n"
        "<blockquote> Você está falando como usuário e não como canal? A permissão "
        "'permanecer anônimo' deve estar desligada! </blockquote>"
    ),
}
_CANNOT_DM_TEXT = {
    "en": "I couldn't send you the configuration menu\n"
    "<blockquote> Send me a message in my private chat so I can do that) </blockquote>",
    "pt": "Não consegui te mandar o menu de configuração\n"
    "<blockquote> Mande uma mensagem no meu chat privado para que eu consiga fazer isso) "
    "</blockquote>",
}

#: The trigger a speaker of each language actually types. `/configurar` is
#: v1's own (Portuguese) spelling of the command and works unchanged in both
#: languages, but an English speaker types the English word.
_CONFIG_COMMAND = {"en": "/config", "pt": "/configurar"}


def test_non_admin_is_denied_and_shown_the_tutorial_video(
    sandbox: SandboxClient, group_id: int, lang: str
) -> None:
    member = sandbox.create_user("Nadia", "nadia")["id"]
    sandbox.join(group_id, member)

    since = len(sandbox.state()["api_calls"])
    sandbox.send_message(group_id, member, text=_CONFIG_COMMAND[lang])

    wait_for(
        lambda: next(
            (
                c
                for c in calls_to(sandbox.state(), "sendMessage", since)
                if _DENIED_TEXT[lang] in c["payload"].get("text", "")
            ),
            None,
        ),
        timeout=15.0,
        description=f"deny the non-admin with the {lang} permission-denied text",
        on_timeout=lambda: describe_recent_calls(sandbox.state()),
    )
    wait_for(
        lambda: next(iter(calls_to(sandbox.state(), "sendVideo", since)), None),
        timeout=15.0,
        description="send the anonymous-mode tutorial video to a genuine non-admin",
        on_timeout=lambda: describe_recent_calls(sandbox.state()),
    )


def test_anonymous_admin_is_not_denied_the_v1_defect_this_port_fixes(
    sandbox: SandboxClient, group_id: int, lang: str
) -> None:
    """v1's regression, reproduced as a negative assertion plus the real,
    fixed behaviour: no denial text, no tutorial video (the two things a
    rejected non-admin gets, asserted for contrast in the sibling test above),
    and instead the graceful "no real id to DM" branch — the *same* branch a
    genuine admin who has simply never DMed the bot would hit, which is
    correct, unmodified Telegram behaviour, not a workaround.
    """
    admin = sandbox.create_user("Ainsley", "ainsley")["id"]
    sandbox.join(group_id, admin)
    sandbox.patch_member(group_id, admin, role="administrator", anonymous=True)

    since = len(sandbox.state()["api_calls"])
    sandbox.send_message(group_id, admin, text=_CONFIG_COMMAND[lang], anonymous=True)

    wait_for(
        lambda: next(
            (
                c
                for c in calls_to(sandbox.state(), "sendMessage", since)
                if _CANNOT_DM_TEXT[lang] in c["payload"].get("text", "")
            ),
            None,
        ),
        timeout=15.0,
        description="treat the anonymous admin as an admin (couldn't-reach-privately, not denied)",
        on_timeout=lambda: describe_recent_calls(sandbox.state()),
    )
    denied = [
        c
        for c in calls_to(sandbox.state(), "sendMessage", since)
        if _DENIED_TEXT[lang] in c["payload"].get("text", "")
    ]
    assert not denied, f"the anonymous admin was rejected like a non-admin: {denied}"
    assert not calls_to(sandbox.state(), "sendVideo", since), (
        "the anonymous-mode tutorial video should never reach a real admin"
    )


def test_an_admin_who_opened_a_dm_gets_the_menu_there(
    sandbox: SandboxClient, group_id: int, lang: str
) -> None:
    """The successful path. `open_dm` is the tester (or the human clicking
    "Open DM" in the web UI) standing in for the admin having pressed Start:
    after it, `bot.send_message(admin_id, ...)` resolves to a real chat and the
    menu lands in the DM rather than the group."""
    admin = sandbox.create_user("Dmitri", "dmitri")["id"]
    sandbox.join(group_id, admin)
    sandbox.patch_member(group_id, admin, role="administrator")
    sandbox.open_dm(admin)

    since = len(sandbox.state()["api_calls"])
    sandbox.send_message(group_id, admin, text=_CONFIG_COMMAND[lang])

    menu = wait_for(
        lambda: next(
            (
                c
                for c in calls_to(sandbox.state(), "sendMessage", since)
                if int(c["payload"].get("chat_id", 0)) == admin and c["payload"].get("reply_markup")
            ),
            None,
        ),
        timeout=15.0,
        description="send the config menu into the admin's DM",
        on_timeout=lambda: describe_recent_calls(sandbox.state()),
    )
    assert int(menu["payload"]["chat_id"]) == admin

    # The fallback the other tests assert must NOT appear: reaching the admin
    # privately is precisely what succeeded here. The menu itself is
    # unlocalised (module docstring), so this checks its absence, not its
    # content — the same in every language.
    assert not [
        c
        for c in calls_to(sandbox.state(), "sendMessage", since)
        if _CANNOT_DM_TEXT[lang] in c["payload"].get("text", "")
    ]


def test_pressing_a_config_button_in_the_dm_answers_the_callback(
    sandbox: SandboxClient, group_id: int, lang: str
) -> None:
    """The menu's buttons are the half a chat transcript cannot vouch for: the
    proof a press was handled is `answerCallbackQuery`, which only the API-call
    log shows."""
    admin = sandbox.create_user("Devi", "devi")["id"]
    sandbox.join(group_id, admin)
    sandbox.patch_member(group_id, admin, role="administrator")
    dm_id = int(sandbox.open_dm(admin)["id"])

    before_dm_messages = len(sandbox.state()["messages"].get(str(dm_id), []))
    sandbox.send_message(group_id, admin, text=_CONFIG_COMMAND[lang])

    menu = wait_for(
        lambda: next(
            (
                message
                for message in messages_in(sandbox.state(), dm_id, before_dm_messages)
                if message["reply_markup"] is not None
            ),
            None,
        ),
        timeout=15.0,
        description="deliver a config menu with buttons into the DM",
        on_timeout=lambda: describe_recent_calls(sandbox.state()),
    )
    button = menu["reply_markup"]["inline_keyboard"][0][0]["callback_data"]

    since = len(sandbox.state()["api_calls"])
    sandbox.press_callback(dm_id, admin, menu["message_id"], button)

    wait_for(
        lambda: calls_to(sandbox.state(), "answerCallbackQuery", since) or None,
        timeout=15.0,
        description=f"answer the config callback {button!r}",
        on_timeout=lambda: describe_recent_calls(sandbox.state()),
    )
