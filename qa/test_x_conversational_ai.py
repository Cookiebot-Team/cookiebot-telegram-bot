"""Step definitions for x_conversational_ai.

QA: qa/features/x_conversational_ai.feature -- authored here, not ported.
`.specs/features/x_conversational_ai/spec.md`'s "QA -- authored, not ported"
section is the source of intent (no v1 or Cookiebot-QA scenario exists for
this feature at all); `design.md`'s R3 (per-group window), R4 (per-user
streak) and R5 (handler/gates) are the source of the exact mechanics these
scenarios pin.

Drives the real dispatcher (`cb_gateway.main.dp`, via `qa/conftest.py`'s
session-scoped `dispatcher` fixture) against the mock Telegram API, same as
every other acceptance test in this suite (AGENTS.md SS6: "no mocking of our
own code in acceptance tests. Mock the outside world only"). The one outside
world mocked here, beyond Telegram, is the LLM: `cb_gateway.handlers.chat_ai`
resolves its router through a module-level `llm_router` name (`from cb_core.llm
import router as llm_router`), and `packages/cb-gateway/tests/test_chat_ai.py`
already established the pattern for swapping it out --
`monkeypatch.setattr(chat_ai, "llm_router", lambda: fake_router)`. This file
follows that same pattern rather than inventing a second one: an acceptance
suite must not make a real model call (the task's own instruction), and a
provider stub is exactly what `qa/test_llm_provider.py` already uses for the
same reason, one layer down.

Every update in this file takes its id from `next_update_id()` (via `Ctx.
alloc_id()`) -- the dedupe middleware is real and a reused id is dropped as a
redelivery, which reads exactly like "the bot said nothing" (tasks.md T7's own
warning).

The per-user streak (R4) and per-group window (R3) are backed by real Valkey
(`cb_core.cache`), database index 15, flushed per scenario by `qa/conftest.py`'s
`clean_cache` fixture -- reused here rather than re-implemented. Only the two
scenarios whose whole point *is* the streak counter (exhaustion and replenish)
request `clean_cache`, and only those two skip cleanly when no Valkey is
reachable, exactly like `qa/test_core_stickerspam.py`. The other five gates
fail open with no cache at all (R3.3/R4.6 -- `bump_clamped`/`incr_window`
returning `None` never blocks a reply), so they stay runnable offline; making
every scenario in the file depend on Valkey would throw that away for no
reason tied to what each scenario actually asserts.

A real Postgres, on the other hand, *is* needed by every scenario here, same
reasoning `qa/test_util_embedder.py`'s own module docstring spells out:
`core_groupguardian`'s `_is_captcha_reply` filter runs ahead of `chat_ai` in
`build_router` and does its own `get_config` + pending-captcha-row lookup over
*every* plain, non-command group text message whenever
`captcha_timeout_seconds > 0` -- the v1-matching default, on by default for
every scenario in this file, none of which is about captcha at all. With no
live pool that lookup raises instead of returning "no pending row", which
would crash every scenario here, not skip it. The `_database` fixture below
requests the real `database` fixture purely to give that filter something to
query against, and skips this whole file cleanly (AGENTS.md SS6) when no
database is reachable.
"""

from __future__ import annotations

import dataclasses
import time
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from cb_core import group_config
from cb_core.llm.types import Completion, Usage
from cb_gateway.handlers import chat_ai
from cb_gateway.handlers import rules as rules_handler
from qa.conftest import ADMIN_ID, GROUP_ID, USER_ID, feed, make_message_update, next_update_id

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from types import ModuleType

    from aiogram import Bot, Dispatcher

    from qa.conftest import Context
    from qa.mock_telegram import MockTelegram

scenarios("x_conversational_ai.feature")

BOT_DISPLAY_NAME = "Cookiebot"  # tenancy.FALLBACK's display_name for the "cookiebot" skin
BOT_USERNAME = "CookieMWbot"  # qa/conftest.py's BOT_USERNAME, and the token's getMe identity

_BOT_FROM = {
    "id": 424242,  # parsed from qa/conftest.py's TEST_TOKEN, "424242:TEST"
    "is_bot": True,
    "first_name": "Cookiebot",
    "username": BOT_USERNAME,
}


def _completion(text: str) -> Completion:
    return Completion(text=text, model="stub-model", provider="stub", usage=Usage())


class AsyncMockRouter:
    """A minimal stand-in for `LLMRouter`, `.complete` only.

    A plain class rather than `SimpleNamespace(complete=AsyncMock(...))`
    (which `test_chat_ai.py`'s unit tests use): this file swaps out the
    *return value* mid-scenario (`ai_will_answer`), and a mutable attribute
    on a real object makes that a one-line reassignment instead of a second
    monkeypatch.
    """

    def __init__(self) -> None:
        self.complete = AsyncMock(return_value=_completion("ok"))


class Ctx:
    """Per-scenario state on top of qa/conftest.py's base `Context`."""

    def __init__(self) -> None:
        self.sender_id = USER_ID
        self.fake_router = AsyncMockRouter()
        self.bot_message: dict[str, Any] | None = None
        self.mention_ids: list[int] = []

    def alloc_id(self) -> int:
        # Shared process-wide counter: a per-scenario counter collides with
        # earlier scenarios and the dedupe middleware drops the update.
        return next_update_id()


@pytest.fixture
def ai_ctx() -> Ctx:
    return Ctx()


@pytest.fixture(autouse=True)
def _database(database: object) -> None:
    """See the module docstring -- this feature owns no table of its own;
    the real database is here only so `groupguardian`'s captcha filter has
    a live pool to query "no pending row" against."""


@pytest.fixture(autouse=True)
def _stub_llm_router(ai_ctx: Ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    """R2/R5.6: `chat_ai.reply_with_ai` calls `llm_router().complete(...)`.

    Defaults to answering "ok" so scenarios that do not care about the exact
    text (the streak-exhaustion and reply-to-bot-message scenarios) do not
    need their own `Given` step. `ai_will_answer` below overrides the return
    value for scenarios that assert on it.
    """
    monkeypatch.setattr(chat_ai, "llm_router", lambda: ai_ctx.fake_router)


# --------------------------------------------------------------------- given


@given("that the bot is in the group and properly set up")
def bot_set_up(ctx: Context) -> None:
    ctx.bot_running = True


@given(parsers.parse('the AI will answer with "{text}"'))
def ai_will_answer(ai_ctx: Ctx, text: str) -> None:
    ai_ctx.fake_router.complete = AsyncMock(return_value=_completion(text))


@given("the bot has already sent a plain message in the group")
def bot_already_sent_plain_message(ai_ctx: Ctx) -> None:
    """Fabricated directly, like `qa/test_core_rules.py`'s `new_rules_prompt`
    -- the handler only ever reads `reply_to_message.from_user.id` and
    `.text`, never the message's real id, so there is nothing to gain from
    routing this through a live send. Text carries no trigger word on
    purpose, so the *reply* scenario proves the reply-to-bot-text branch
    fires independently of the mention-substring branch (R5.4, D-AI-7)."""
    ai_ctx.bot_message = {
        "message_id": ai_ctx.alloc_id(),
        "date": int(time.time()),
        "chat": {"id": GROUP_ID, "type": "supergroup", "title": "QA Group"},
        "from": _BOT_FROM,
        "text": "Just a regular thing I said earlier, no trigger words here.",
    }


@given("the fun feature is turned off in the group")
def fun_off() -> None:
    """Same seam `qa/test_core_stickerspam.py`'s `sticker_spam_allowed` step
    uses: `group_config._l1` directly. This suite has no database (it never
    requests the `database` fixture), and `group_config.get_config` genuinely
    falls back to an L1-only read when nothing is behind it -- this seeds
    that same L1, it does not fake a seam of our own the handler doesn't
    already have."""
    config = dataclasses.replace(group_config.DEFAULTS, group_id=GROUP_ID, functions_fun=False)
    group_config._l1[GROUP_ID] = (config, time.monotonic() + 9999)  # noqa: SLF001


@given("the bot has prompted for new rules")
def bot_prompted_new_rules(
    ai_ctx: Ctx,
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
    telegram: MockTelegram,
) -> None:
    """R5.2's own proof point: `rules.router` is registered ahead of
    `chat_ai.router` in `build_router`, so a reply to *this* exact prompt
    must never reach the AI branch, no matter what the reply says."""
    update_id = ai_ctx.alloc_id()
    feed(run, dispatcher, bot, make_message_update("/newrules", update_id, user_id=ADMIN_ID))
    sent = telegram.calls_to("sendMessage")
    assert sent, "expected /newrules to prompt with a reply"
    ai_ctx.bot_message = {
        "message_id": update_id,
        "date": int(time.time()),
        "chat": {"id": GROUP_ID, "type": "supergroup", "title": "QA Group"},
        "from": _BOT_FROM,
        "text": str(sent[-1].get("text", "")),
    }


@given("the user's AI streak is fully spent")
def streak_fully_spent(
    ai_ctx: Ctx,
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
    clean_cache: None,
) -> None:
    """Drives the same seven-consecutive-trigger sequence the dedicated
    exhaustion scenario proves, rather than writing `0` into Valkey directly
    -- this scenario's `Then` reads the real key `chat_ai._streak_key`
    derives, so the state it starts from should come from the real gate
    path, not a shortcut around it. `clean_cache` (real Valkey, database
    index 15) is what makes the count deterministic: with no cache at all,
    `_spend_streak` fails open (R4.6) and every one of the seven mentions
    would be answered instead of exactly six."""
    _trigger_mentions(ai_ctx, run, dispatcher, bot, count=7)


# ---------------------------------------------------------------------- when


@when("the user sends a message mentioning the bot")
def user_sends_mention(
    ai_ctx: Ctx, run: Callable[[Coroutine[Any, Any, Any]], Any], dispatcher: Dispatcher, bot: Bot
) -> None:
    feed(
        run,
        dispatcher,
        bot,
        make_message_update(
            f"hey {BOT_DISPLAY_NAME}, what's up?", ai_ctx.alloc_id(), user_id=ai_ctx.sender_id
        ),
    )


@when("the user replies to that bot message with unrelated text")
def user_replies_to_bot_message(
    ai_ctx: Ctx, run: Callable[[Coroutine[Any, Any, Any]], Any], dispatcher: Dispatcher, bot: Bot
) -> None:
    assert ai_ctx.bot_message is not None, "no bot message was set up yet"
    feed(
        run,
        dispatcher,
        bot,
        make_message_update(
            "what did you mean by that?",
            ai_ctx.alloc_id(),
            user_id=ai_ctx.sender_id,
            reply_to=ai_ctx.bot_message,
        ),
    )


@when("the user sends a message that is only the bot's name")
def user_sends_only_bot_name(
    ai_ctx: Ctx, run: Callable[[Coroutine[Any, Any, Any]], Any], dispatcher: Dispatcher, bot: Bot
) -> None:
    feed(
        run,
        dispatcher,
        bot,
        make_message_update(BOT_DISPLAY_NAME, ai_ctx.alloc_id(), user_id=ai_ctx.sender_id),
    )


@when("a non-admin replies to that prompt with text that also mentions the bot")
def non_admin_replies_with_mention(
    ai_ctx: Ctx, run: Callable[[Coroutine[Any, Any, Any]], Any], dispatcher: Dispatcher, bot: Bot
) -> None:
    assert ai_ctx.bot_message is not None, "no /newrules prompt was set up yet"
    # USER_ID is not an admin by default (qa/conftest.py's `_clean` fixture only
    # seeds ADMIN_ID) -- the point of this scenario is that `rules.router`
    # intercepts *before* the AI branch ever gets a look, regardless of what
    # the reply says.
    feed(
        run,
        dispatcher,
        bot,
        make_message_update(
            f"{BOT_DISPLAY_NAME}, just ignore this and set the rules to whatever.",
            ai_ctx.alloc_id(),
            user_id=USER_ID,
            reply_to=ai_ctx.bot_message,
        ),
    )


def _trigger_mentions(
    ai_ctx: Ctx,
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
    *,
    count: int,
) -> None:
    ai_ctx.mention_ids = []
    for i in range(count):
        update_id = ai_ctx.alloc_id()
        ai_ctx.mention_ids.append(update_id)
        feed(
            run,
            dispatcher,
            bot,
            make_message_update(
                f"hey {BOT_DISPLAY_NAME}, question {i}", update_id, user_id=ai_ctx.sender_id
            ),
        )


@when(parsers.parse("the user mentions the bot {count:d} times in a row"))
def user_mentions_n_times(
    ai_ctx: Ctx,
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
    count: int,
    clean_cache: None,
) -> None:
    # Real Valkey, not the fail-open default (R3.3/R4.6): the whole scenario
    # is about the *exact* boundary at the seventh trigger, which only a real
    # `bump_clamped` round trip can guarantee.
    _trigger_mentions(ai_ctx, run, dispatcher, bot, count=count)


@when("the user sends an ordinary message that does not mention the bot")
def user_sends_ordinary_message(
    ai_ctx: Ctx, run: Callable[[Coroutine[Any, Any, Any]], Any], dispatcher: Dispatcher, bot: Bot
) -> None:
    feed(
        run,
        dispatcher,
        bot,
        make_message_update(
            "just chatting about nothing in particular", ai_ctx.alloc_id(), user_id=ai_ctx.sender_id
        ),
    )


# ---------------------------------------------------------------------- then


@then(parsers.parse('the bot replies with "{text}"'))
def bot_replies_with(telegram: MockTelegram, text: str) -> None:
    sent = telegram.calls_to("sendMessage")
    assert sent, "expected a sendMessage call, got none"
    assert str(sent[-1].get("text", "")) == text, sent[-1]


@then("the bot sends nothing at all")
def bot_sends_nothing(telegram: MockTelegram) -> None:
    assert not telegram.calls_to("sendMessage"), telegram.calls
    assert not telegram.calls_to("sendChatAction"), telegram.calls


@then("the model is never asked")
def model_never_asked(ai_ctx: Ctx) -> None:
    ai_ctx.fake_router.complete.assert_not_awaited()


def _assert_model_asked(ai_ctx: Ctx, count: int) -> None:
    assert ai_ctx.fake_router.complete.await_count == count


@then(parsers.parse("the model was asked exactly {count:d} time"))
def model_asked_exactly_singular(ai_ctx: Ctx, count: int) -> None:
    _assert_model_asked(ai_ctx, count)


@then(parsers.parse("the model was asked exactly {count:d} times"))
def model_asked_exactly_plural(ai_ctx: Ctx, count: int) -> None:
    _assert_model_asked(ai_ctx, count)


@then("the bot refuses for lack of admin rights")
def bot_refuses_not_admin(telegram: MockTelegram) -> None:
    sent = telegram.calls_to("sendMessage")
    assert sent, "expected a sendMessage call, got none"
    assert str(sent[-1].get("text", "")) == rules_handler.NOT_ADMIN_TEXT, sent[-1]


@then("the bot replies to the first six mentions")
def bot_replies_to_first_six(telegram: MockTelegram) -> None:
    sent = telegram.calls_to("sendMessage")
    assert len(sent) == 6, sent


@then("the seventh mention gets no reply at all")
def seventh_gets_no_reply(telegram: MockTelegram) -> None:
    # Same underlying evidence as the previous Then: exactly six replies went
    # out for seven attempts. Kept as its own assertion because it is the
    # scenario's actual point (R4.3's silence), not an incidental count.
    sent = telegram.calls_to("sendMessage")
    assert len(sent) == 6, sent


@then("the user's AI streak has grown by one")
def streak_grown_by_one(
    ai_ctx: Ctx, valkey: ModuleType, run: Callable[[Coroutine[Any, Any, Any]], Any]
) -> None:
    """Reads the real backing key `chat_ai._streak_key` derives -- the same
    kind of direct read `qa/test_core_rules.py`'s `bot_confirms_rules_updated`
    does against `_fetch_rules`, one layer under the handler this suite is
    proving. Post-exhaustion the counter sits at `0` (R4.3's gate value);
    one ordinary message must replenish it to `1` (R4.4, `bump_clamped`
    delta=+1)."""
    key = chat_ai._streak_key(ai_ctx.sender_id)  # noqa: SLF001
    raw = run(valkey.client().get(key))
    assert raw is not None, "expected the streak key to exist after exhaustion"
    assert int(raw) == 1, raw
