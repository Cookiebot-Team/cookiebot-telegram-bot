"""core_stickerspam — warn at the limit, delete past it, over real HTTP.

The sender here is an admin, deliberately: `mediarestrict` runs earlier in the
join chain and would otherwise intercept a *fresh* member's stickers as
restricted media before `stickerspam` ever saw them (both handlers react to
`F.sticker`; aiogram stops at whichever router acts first —
`cb_gateway/handlers/mediarestrict.py`'s own module docstring). An existing
admin has no `group_members` row at all from this suite's perspective, so
`mediarestrict` skips immediately regardless — the same reason
stickerspam.py's own docstring calls out "no admin exemption... an admin's
own sticker flood counts and gets deleted the same as anyone else's".

Runs once per language (`qa/e2e/conftest.py`'s `lang` fixture): the warning
text (`locales.get("flood_stickers", lang)`) is genuinely localised, so the
assertion below checks the real catalog value for the group's own language
rather than merely "some sendMessage happened".
"""

from __future__ import annotations

import pytest

from cb_core import locales
from qa.e2e.client import SandboxClient, calls_to, describe_recent_calls, wait_for

pytestmark = pytest.mark.e2e

# GroupConfig.DEFAULTS.sticker_spam_limit (packages/cb-core/src/cb_core/group_config.py) —
# this group never wrote a group_configs row of its own beyond the captcha
# override `qa/e2e/conftest.py`'s `group_id` fixture applies, so the v1
# default (5) is what the handler is actually gating on.
_STICKER_SPAM_LIMIT = 5


def test_sticker_flood_warns_at_the_limit_then_deletes(
    sandbox: SandboxClient, group_id: int, lang: str
) -> None:
    admin = sandbox.create_user("Stevie", "stevie")["id"]
    sandbox.join(group_id, admin)
    sandbox.patch_member(group_id, admin, role="administrator")

    since = len(sandbox.state()["api_calls"])
    for _ in range(_STICKER_SPAM_LIMIT):
        sandbox.send_message(group_id, admin, media="sticker")

    expected_warning = locales.get("flood_stickers", lang)
    wait_for(
        lambda: next(
            (
                c
                for c in calls_to(sandbox.state(), "sendMessage", since)
                if c["payload"].get("text") == expected_warning
            ),
            None,
        ),
        timeout=15.0,
        description=f"warn in the group's own language once the {_STICKER_SPAM_LIMIT}th sticker reaches the limit",
        on_timeout=lambda: describe_recent_calls(sandbox.state()),
    )
    assert not calls_to(sandbox.state(), "deleteMessage", since), (
        "nothing should be deleted at exactly the limit"
    )

    since = len(sandbox.state()["api_calls"])
    over_limit = sandbox.send_message(group_id, admin, media="sticker")

    wait_for(
        lambda: next(
            (
                c
                for c in calls_to(sandbox.state(), "deleteMessage", since)
                if int(c["payload"].get("message_id", -1)) == over_limit["message_id"]
            ),
            None,
        ),
        timeout=15.0,
        description="delete the sticker that pushed the count past the limit",
        on_timeout=lambda: describe_recent_calls(sandbox.state()),
    )
