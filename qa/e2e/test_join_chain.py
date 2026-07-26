"""The join chain — order matters: doomlist intercepts before welcome ever
runs, and a self-join that nobody intercepts falls through to it
(`cb_gateway/handlers/__init__.py`'s own module docstring: aiogram stops at
the first router that *handles* an update, so registration order is the
behaviour). Two scenarios prove both ends of that chain over real HTTP.

Captcha — the chain's *first* link — is not exercised here, because with it on
nothing downstream of it runs at all and neither scenario below would be
reachable. The `group_id` fixture turns it off for that reason;
`test_captcha.py` turns it back on for the one test that is about captcha.

Both scenarios run once per language (`qa/e2e/conftest.py`'s `lang` fixture).
`welcome_user` and doomlist's `ban`/`ban_cas` are all genuinely localised
(`locales.get(..., lang, ...)`), so both this file's assertions read the
expected text back from the catalog for the group's own language rather than
pinning `"en"`.
"""

from __future__ import annotations

import pytest

from cb_core import locales
from qa.e2e.client import SandboxClient, calls_to, describe_recent_calls, wait_for

pytestmark = pytest.mark.e2e


def test_a_plain_self_join_falls_through_to_the_welcome_message(
    sandbox: SandboxClient, group_id: int, lang: str
) -> None:
    """No doomlist hit, captcha off (the `group_id` fixture's default) — the
    only router left standing in the join chain is `welcome.router`.
    """
    newcomer = sandbox.create_user("Newt", "newt")["id"]

    since = len(sandbox.state()["api_calls"])
    sandbox.join(group_id, newcomer)

    # v1's default welcome (GroupShield.py:154-158): the group's own title
    # substituted into "welcome_user" — no group_welcomes row exists yet for
    # this fresh group, so this is the fallback text, not a custom one.
    chat_title = next(c["title"] for c in sandbox.state()["chats"] if c["id"] == group_id)
    expected = locales.get("welcome_user", lang, user=chat_title)
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
        description="send the default welcome message to a plain self-join",
        on_timeout=lambda: describe_recent_calls(sandbox.state()),
    )
    # And specifically not banned — the doomlist scenario below is what
    # exercises banChatMember; a plain newcomer must never see it.
    assert not calls_to(sandbox.state(), "banChatMember", since)


def test_a_doomlisted_name_is_banned_before_welcome_ever_runs(
    sandbox: SandboxClient, group_id: int, lang: str
) -> None:
    """`check_local_blacklist` (`cb_gateway/handlers/doomlist.py`) matches a
    forbidden glyph in the joiner's name with no network call and no seeded
    Postgres row — the one doomlist branch this suite can trigger
    deterministically; the `cas.chat`/burrbot branches depend on a live
    vendor's opinion of a sandbox-only id and cannot be. `check_cas` still
    runs first in the real dispatch order and makes one real, harmless
    network call before falling through (fails open on any non-hit answer),
    which is why this scenario's own timeout is a little more generous.
    """
    raider = sandbox.create_user("卐Raider", "raider_incoming")["id"]

    since = len(sandbox.state()["api_calls"])
    sandbox.join(group_id, raider)

    wait_for(
        lambda: next(
            (
                c
                for c in calls_to(sandbox.state(), "banChatMember", since)
                if int(c["payload"].get("user_id", -1)) == raider
            ),
            None,
        ),
        timeout=20.0,
        description="ban the doomlisted newcomer",
        on_timeout=lambda: describe_recent_calls(sandbox.state()),
    )
    # doomlist.py's local-blacklist hit always answers with the "ban" locale
    # key (never "ban_cas", which only the CAS branch uses) — full equality,
    # not a substring, is possible here because `t(ctx, hit_key)` sends the
    # catalog value verbatim with no further formatting.
    expected_ban_text = locales.get("ban", lang)
    wait_for(
        lambda: next(
            (
                c
                for c in calls_to(sandbox.state(), "sendMessage", since)
                if c["payload"].get("text") == expected_ban_text
            ),
            None,
        ),
        timeout=5.0,
        description="announce the ban in the group in the group's own language",
        on_timeout=lambda: describe_recent_calls(sandbox.state()),
    )
    # Welcome never got a turn — doomlist's router handled the update first.
    chat_title = next(c["title"] for c in sandbox.state()["chats"] if c["id"] == group_id)
    welcome_text = locales.get("welcome_user", lang, user=chat_title)
    assert not any(
        c["payload"].get("text") == welcome_text
        for c in calls_to(sandbox.state(), "sendMessage", since)
    )
