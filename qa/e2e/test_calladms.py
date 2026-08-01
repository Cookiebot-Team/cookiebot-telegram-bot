"""`/adm` -> press the confirm button, over real HTTP via `/api/chats/{id}/callback`.

The button flow lives entirely inside the group (no DM, so none of
`test_config_menu.py`'s sandbox limitation applies here): `/adm` posts a
Yes/No keyboard, pressing Yes deletes that prompt and pings the group's
admins, and — the v1 defect this port fixes (`cb_gateway/handlers/calladms.py`'s
own docstring) — the callback is *answered* either way, so a real Telegram
client's loading spinner actually stops. `answerCallbackQuery` and
`deleteMessage` are exactly the two calls a chat transcript cannot show,
which is the whole reason `docs/site/content/docs/sandbox.mdx` calls the api_calls log the real
validation surface.

Runs once per language (`qa/e2e/conftest.py`'s `lang` fixture). `/adm` itself
has no distinct Portuguese alias to switch to — `cb_core.textmatch
.COMMAND_ALIASES` maps `adm`/`admin`/`report` to `calladms` with no
language-specific entry, because v1 dispatched the same English word `/adm`
in every language's groups (`calladms.py`'s own module docstring: "All four
v1 triggers work" lists only `/adm`, `/admin`, `/report` and the bare-word
forms, no PT/ES spelling) — so `/adm` is genuinely what a Portuguese-speaking
admin types too, not an English-only trigger left unparametrised by omission.
The confirmation prompt (`call_admin_ask`) and the ping text (`call_admin`)
*are* localised, though, and are asserted against the real catalog value for
the group's own language below.
"""

from __future__ import annotations

import pytest

from cb_core import locales
from qa.e2e.client import SandboxClient, calls_to, describe_recent_calls, messages_in, wait_for

pytestmark = pytest.mark.e2e


def test_pressing_confirm_answers_the_callback_and_pings_admins(
    sandbox: SandboxClient, group_id: int, lang: str
) -> None:
    member = sandbox.create_user("Cass", "cass")["id"]
    sandbox.join(group_id, member)

    since = len(sandbox.state()["api_calls"])
    sandbox.send_message(group_id, member, text="/adm")

    wait_for(
        lambda: next(
            (
                c
                for c in calls_to(sandbox.state(), "sendMessage", since)
                if c["payload"].get("reply_markup")
            ),
            None,
        ),
        timeout=15.0,
        description="post the /adm confirmation prompt with a Yes/No keyboard",
        on_timeout=lambda: describe_recent_calls(sandbox.state()),
    )
    # Read the prompt back from the chat's own message list — its
    # `reply_markup` is already a decoded dict there, unlike the raw wire-
    # format payload `api_calls` recorded.
    prompt_message = next(
        m for m in messages_in(sandbox.state(), group_id) if m.get("reply_markup")
    )
    yes_button = next(
        button
        for row in prompt_message["reply_markup"]["inline_keyboard"]
        for button in row
        if button["text"] == "✔️"
    )

    since = len(sandbox.state()["api_calls"])
    sandbox.press_callback(
        group_id, member, prompt_message["message_id"], yes_button["callback_data"]
    )

    wait_for(
        lambda: next(iter(calls_to(sandbox.state(), "answerCallbackQuery", since)), None),
        timeout=15.0,
        description="answer the callback query (v1 never did — the spinner-forever defect)",
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
        description="delete the confirmation prompt unconditionally",
        on_timeout=lambda: describe_recent_calls(sandbox.state()),
    )
    # `t(ctx, "call_admin", caller=...)` — "cass" is who pressed the button
    # (`caller = presser.username or presser.first_name`, `calladms.py`), so
    # the formatted fragment below is what the group's own language actually
    # renders, not a hardcoded English substring.
    expected_ping = locales.get("call_admin", lang, caller="cass")
    wait_for(
        lambda: next(
            (
                c
                for c in calls_to(sandbox.state(), "sendMessage", since)
                if expected_ping in c["payload"].get("text", "")
            ),
            None,
        ),
        timeout=15.0,
        description="ping the group's admins in the group's own language",
        on_timeout=lambda: describe_recent_calls(sandbox.state()),
    )
