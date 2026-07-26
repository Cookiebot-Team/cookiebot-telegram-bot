"""core_mediarestrict — a fresh member's media gets deleted, over real HTTP.

v2's mechanism is reactive, not preventive (`cb_gateway/handlers/mediarestrict
.py`'s own module docstring): it records `group_members.joined_at` on join,
then on every later message carrying restricted content compares
`now() - joined_at` against the group's configured window (v1 default 600s,
`GroupConfig.DEFAULTS.media_restrict_seconds`) and deletes-after-the-fact
rather than natively muting. Joining and immediately sending a photo, well
inside that window, is exactly the case the contract names.

Both tests run once per language (`qa/e2e/conftest.py`'s `lang` fixture). The
restriction notice (`locales.get("restrict_message", lang, ...)`) is
genuinely localised, so the first test asserts against the group's own
language's catalog value. `/isalive`'s reply text is not — `isalive.py`
hardcodes "Alive and operational" with no `locales`/`t()` call at all, in
every language — but the *trigger* the second test sends still switches per
language (`/isalive` vs `/tavivo`, both real `COMMAND_ALIASES` entries),
because a Portuguese-speaking admin proving the same ordering fact would type
the Portuguese command, not the English one, even though the reply it waits
for never changes.
"""

from __future__ import annotations

import pytest

from cb_core import locales
from qa.e2e.client import SandboxClient, calls_to, describe_recent_calls, wait_for

pytestmark = pytest.mark.e2e

# GroupConfig.DEFAULTS.media_restrict_seconds = 600 -> round(600 / 60) = 10,
# the exact number `_restrict_minutes` renders into the reply text.
_RESTRICT_MINUTES = 10

#: The trigger a speaker of each language actually types
#: (`COMMAND_ALIASES["tavivo"] = "isalive"`). Only used as a synchronisation
#: signal below — `isalive.py`'s own reply text never changes with it.
_ISALIVE_COMMAND = {"en": "/isalive", "pt": "/tavivo"}


def test_a_fresh_members_photo_is_deleted_within_the_restriction_window(
    sandbox: SandboxClient, group_id: int, lang: str
) -> None:
    newcomer = sandbox.create_user("Percy", "percy")["id"]

    since = len(sandbox.state()["api_calls"])
    sandbox.join(group_id, newcomer)
    # The join itself must not be treated as restricted content — only what
    # follows it is.
    assert not calls_to(sandbox.state(), "deleteMessage", since)

    since = len(sandbox.state()["api_calls"])
    photo = sandbox.send_message(group_id, newcomer, media="photo")

    wait_for(
        lambda: next(
            (
                c
                for c in calls_to(sandbox.state(), "deleteMessage", since)
                if int(c["payload"].get("message_id", -1)) == photo["message_id"]
            ),
            None,
        ),
        timeout=15.0,
        description="delete a fresh member's photo",
        on_timeout=lambda: describe_recent_calls(sandbox.state()),
    )
    expected_notice = locales.get("restrict_message", lang, time=_RESTRICT_MINUTES)
    wait_for(
        lambda: next(
            (
                c
                for c in calls_to(sandbox.state(), "sendMessage", since)
                if c["payload"].get("text") == expected_notice
            ),
            None,
        ),
        timeout=15.0,
        description="warn the group about the restriction",
        on_timeout=lambda: describe_recent_calls(sandbox.state()),
    )


def test_an_admins_photo_is_never_restricted(
    sandbox: SandboxClient, group_id: int, lang: str
) -> None:
    """An absence needs a positive signal to bound the wait on, or the check
    races ahead of the bot ever seeing the update. `/isalive` right after the
    photo is that signal: aiogram processes one bot's updates strictly in
    order (proven by every other scenario in this suite that depends on it,
    e.g. `test_rules.py`'s prompt-then-reply flow), so `/isalive`'s own reply
    cannot appear until the photo's update has already been fully dispatched
    and handled — at which point "no deleteMessage for it" is a fact, not a
    guess made after an arbitrary sleep.
    """
    admin = sandbox.create_user("Aiden", "aiden")["id"]
    sandbox.join(group_id, admin)
    sandbox.patch_member(group_id, admin, role="administrator")

    since = len(sandbox.state()["api_calls"])
    photo = sandbox.send_message(group_id, admin, media="photo")
    sandbox.send_message(group_id, admin, text=_ISALIVE_COMMAND[lang])

    wait_for(
        lambda: next(
            (
                c
                for c in calls_to(sandbox.state(), "sendMessage", since)
                if "Alive and operational" in c["payload"].get("text", "")
            ),
            None,
        ),
        timeout=15.0,
        description="answer /isalive, proving the preceding photo update already ran",
        on_timeout=lambda: describe_recent_calls(sandbox.state()),
    )
    assert not any(
        int(c["payload"].get("message_id", -1)) == photo["message_id"]
        for c in calls_to(sandbox.state(), "deleteMessage", since)
    )
