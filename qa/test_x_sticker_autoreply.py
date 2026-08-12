"""Step definitions for x_sticker_autoreply.

QA: qa/features/x_sticker_autoreply.feature (authored here — Cookiebot-QA has
no scenario for this feature; see that file's own header). Contract: the
module docstring on `cb_gateway.handlers.sticker_autoreply` and on migration
`0009_sticker_pool`.

Drives the real dispatcher against the mock Telegram API and a real
`sticker_pool` table — `sticker_autoreply.py` reads and writes it directly
through `cb_core.db` (AGENTS.md §6 forbids mocking our own code in an
acceptance test), so every "is it pooled" assertion below reads the actual
row rather than a stand-in for one. `sticker_pool` has no `group_id`
(migration 0009's own docstring: it is a reference table, not distributed),
so cleanup is keyed on the sticker `file_id`s this suite itself creates,
never on `GROUP_ID`.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Coroutine, Iterator
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from pytest_bdd import given, scenarios, then, when

from cb_core import group_config
from qa.conftest import (
    BOT_USERNAME,
    GROUP_ID,
    USER_ID,
    Context,
    feed,
    make_message_update,
    next_update_id,
)
from qa.mock_telegram import MockTelegram

scenarios("x_sticker_autoreply.feature")

Run = Callable[[Coroutine[Any, Any, Any]], Any]

# qa/conftest.py's TEST_TOKEN is "424242:TEST" -- aiogram's Bot.id parses the
# numeric prefix, so this is the identity `_is_reply_to_bot` compares against.
_BOT_FROM = {"id": 424242, "is_bot": True, "first_name": "Cookiebot", "username": BOT_USERNAME}

_CLEAN_SET_NAME = "cleanpack1"
_CLEAN_FILE_ID = f"sticker-{_CLEAN_SET_NAME}"
_NSFW_SET_NAME = "nsfwpack1"
_NSFW_FILE_ID = f"sticker-{_NSFW_SET_NAME}"
_BANNED_EMOJI_SET_NAME = "bannedemojipack1"
_BANNED_EMOJI_FILE_ID = f"sticker-{_BANNED_EMOJI_SET_NAME}"
_SEED_SET_NAME = "seedpack1"
_SEED_FILE_ID = f"sticker-{_SEED_SET_NAME}"
_REPLY_SET_NAME = "replypack1"
_REPLY_FILE_ID = f"sticker-{_REPLY_SET_NAME}"

# Every file_id this suite itself ever writes -- including _REPLY_FILE_ID,
# which the reply-to-the-bot scenario pools as a side effect of its own
# sticker_update handler (pooling and replying are independent branches of
# the same v1 dispatch, module docstring), not just the ones a `Then` step
# checks directly.
_ALL_TEST_FILE_IDS = (
    _CLEAN_FILE_ID,
    _NSFW_FILE_ID,
    _BANNED_EMOJI_FILE_ID,
    _SEED_FILE_ID,
    _REPLY_FILE_ID,
)


class Ctx:
    """Per-scenario state on top of qa/conftest.py's base `Context`."""

    def __init__(self) -> None:
        self.chat_title: str | None = None

    def alloc_id(self) -> int:
        # Shared process-wide counter: a per-scenario counter collides with
        # earlier scenarios and the dedupe middleware drops the update.
        return next_update_id()


@pytest.fixture
def sa_ctx() -> Ctx:
    return Ctx()


@pytest.fixture(autouse=True)
def _clean_sticker_pool(database: Any, run: Run) -> Iterator[None]:
    """The real `sticker_pool` reference table, cleaned around each scenario
    for exactly the file_ids this suite creates.

    Deliberately scoped to `_ALL_TEST_FILE_IDS`, never a blanket `TRUNCATE` or
    `DELETE FROM sticker_pool`: unlike every per-group table this suite's
    sibling fixtures clean by `WHERE group_id = $1`
    (`qa/test_fun_random.py`'s `_clean_media`), `sticker_pool` is a reference
    table with no `group_id` at all (migration 0009's own docstring) — it is
    genuinely global, shared with every other group in a real deployment and,
    on a machine where the importer or another suite has already run, with
    rows this test never wrote. Deleting the whole table would destroy real
    data no other fixture in this codebase would ever risk; deleting only
    this suite's own rows is the honest equivalent of `_clean_media`'s
    `group_id` scope for a table that has no such scope to filter on. This
    is also why `bot_replies_with_sticker` below asserts pool *membership*
    rather than equality to a specific seeded row — the table's real
    population is never this suite's to assume empty.

    Requires the real database (`database` fixture skips cleanly when
    unreachable, same as every other DB-backed acceptance suite).
    """
    from cb_core import db

    stmt = "DELETE FROM sticker_pool WHERE file_id = ANY($1::text[])"
    run(db.execute(stmt, list(_ALL_TEST_FILE_IDS), name="qa_clean_sticker_pool"))
    yield
    run(db.execute(stmt, list(_ALL_TEST_FILE_IDS), name="qa_clean_sticker_pool"))


@pytest.fixture(autouse=True)
def _reset_config(run: Run) -> Iterator[None]:
    """`group_config._l1` is process-global and the row lives in a real,
    shared Postgres; scenarios below flip `functions_fun`/`sfw` on the same
    `GROUP_ID` every other DB-backed suite reuses. Left set, whichever
    scenario runs next (in this file or, under randomised ordering, any
    other) inherits a bot with fun switched off or sfw on and fails
    somewhere unrelated to the cause — the exact leak `qa/test_fun_random.py`
    and `qa/test_core_mediarestrict.py` already guard against, and the one
    `qa/test_x_unearth.py`'s own `_reset_config` names as "only visible with
    a database attached."

    Every scenario in this file takes `database` (via the autouse
    `_clean_sticker_pool` above), so there is always a pool to write
    through; `contextlib.suppress` here is not load-bearing today, kept only
    so this fixture degrades the same way if a future scenario is added that
    does not.
    """
    yield
    with contextlib.suppress(Exception):
        run(group_config.set_config(GROUP_ID, functions_fun=True, sfw=True))
    group_config._l1.clear()  # noqa: SLF001


def _seed_pool(run: Run, file_id: str) -> None:
    from cb_core import db

    run(
        db.execute(
            "INSERT INTO sticker_pool (file_id) VALUES ($1) ON CONFLICT (file_id) DO NOTHING",
            file_id,
            name="qa_seed_sticker_pool",
        )
    )


def _is_pooled(run: Run, file_id: str) -> bool:
    from cb_core import db

    row = run(
        db.fetchrow(
            "SELECT 1 FROM sticker_pool WHERE file_id = $1", file_id, name="qa_check_sticker_pool"
        )
    )
    return row is not None


# --------------------------------------------------------------------- given


@given("that the bot is in the group and properly set up")
def bot_set_up(ctx: Context) -> None:
    ctx.bot_running = True


@given("the group is configured sfw")
def group_is_sfw(run: Run) -> None:
    run(group_config.set_config(GROUP_ID, sfw=True))


@given("the group's title flags it as NSFW")
def group_title_is_nsfw(sa_ctx: Ctx, run: Run) -> None:
    run(group_config.set_config(GROUP_ID, sfw=True))
    # v1's write-side title check reads the incoming message's own chat
    # title, not a stored config field (SocialContent.py:211) -- recorded
    # here for the "When" step to actually send with.
    sa_ctx.chat_title = "NSFW Fan Group \U0001f51e"


@given("the pool already has a pooled sticker")
def pool_has_a_sticker(run: Run) -> None:
    _seed_pool(run, _SEED_FILE_ID)


@given("that fun functions are disabled for the group")
def fun_disabled(run: Run) -> None:
    run(group_config.set_config(GROUP_ID, functions_fun=False))


# ---------------------------------------------------------------------- when


@when("a user sends a sticker from a clean, alphanumeric pack")
def user_sends_clean_sticker(sa_ctx: Ctx, run: Run, dispatcher: Dispatcher, bot: Bot) -> None:
    feed(
        run,
        dispatcher,
        bot,
        make_message_update(
            None,
            sa_ctx.alloc_id(),
            chat_title=sa_ctx.chat_title,
            sticker=_CLEAN_SET_NAME,
            sticker_emoji="\U0001f600",  # 😀, not on the banned list
        ),
    )


@when("a user sends a sticker whose emoji is on the banned list")
def user_sends_banned_emoji_sticker(
    sa_ctx: Ctx, run: Run, dispatcher: Dispatcher, bot: Bot
) -> None:
    feed(
        run,
        dispatcher,
        bot,
        make_message_update(
            None,
            sa_ctx.alloc_id(),
            chat_title=sa_ctx.chat_title,
            sticker=_BANNED_EMOJI_SET_NAME,
            sticker_emoji="\U0001f346",  # 🍆, SocialContent.py:210's own list
        ),
    )


@when("a user replies to the bot with a sticker")
def user_replies_to_bot_with_sticker(
    sa_ctx: Ctx, run: Run, dispatcher: Dispatcher, bot: Bot
) -> None:
    reply_to = {
        "message_id": sa_ctx.alloc_id(),
        "date": 0,
        "chat": {"id": GROUP_ID, "type": "supergroup", "title": "QA Group"},
        "from": _BOT_FROM,
        "text": "here's a sticker for you",
    }
    feed(
        run,
        dispatcher,
        bot,
        make_message_update(
            None,
            sa_ctx.alloc_id(),
            sticker=_REPLY_SET_NAME,
            sticker_emoji="\U0001f600",
            reply_to=reply_to,
        ),
    )


@when("a user replies to another user with a sticker")
def user_replies_to_another_user_with_sticker(
    sa_ctx: Ctx, run: Run, dispatcher: Dispatcher, bot: Bot
) -> None:
    reply_to = {
        "message_id": sa_ctx.alloc_id(),
        "date": 0,
        "chat": {"id": GROUP_ID, "type": "supergroup", "title": "QA Group"},
        "from": {"id": USER_ID + 2, "is_bot": False, "first_name": "Other", "username": "other"},
        "text": "not the bot",
    }
    feed(
        run,
        dispatcher,
        bot,
        make_message_update(
            None,
            sa_ctx.alloc_id(),
            sticker=_REPLY_SET_NAME,
            sticker_emoji="\U0001f600",
            reply_to=reply_to,
        ),
    )


# ---------------------------------------------------------------------- then


@then("the sticker is added to the pool")
def sticker_is_pooled(run: Run) -> None:
    assert _is_pooled(run, _CLEAN_FILE_ID), "expected the sticker to be in sticker_pool"


@then("the sticker is not added to the pool")
def sticker_is_not_pooled(run: Run) -> None:
    assert not _is_pooled(run, _NSFW_FILE_ID) and not _is_pooled(run, _BANNED_EMOJI_FILE_ID), (
        "expected the sticker to be absent from sticker_pool"
    )


@then("the bot replies with a sticker from the pool")
def bot_replies_with_sticker(run: Run, telegram: MockTelegram) -> None:
    """Membership, not equality to `_SEED_FILE_ID` -- `sticker_pool` is a
    real, global, shared table (`_clean_sticker_pool`'s own docstring), so
    the row `ORDER BY random()` actually returns may be this suite's seed or
    any other row already in the table; both are a correct answer to "reply
    with a sticker from the pool"."""
    sent = telegram.calls_to("sendSticker")
    assert sent, "expected a sendSticker call, got none"
    file_id = sent[-1].get("sticker")
    assert _is_pooled(run, file_id), f"{file_id!r} was sent but is not a row in sticker_pool"


@then("the user receives no sticker reply")
def no_sticker_reply(telegram: MockTelegram) -> None:
    assert not telegram.calls_to("sendSticker")
