"""QA harness.

The whole suite runs against MockTelegram on a single session event loop, so
steps stay synchronous (pytest-bdd generates sync tests) while the mock server
and the aiogram client share one loop and can talk to each other inside a single
`run()` call.

No Postgres and no Valkey are required: the dedupe middleware degrades to its
in-process LRU when the cache is unavailable, and the event recorder buffers in
memory. Infra-backed scenarios arrive with M1.
"""

from __future__ import annotations

import asyncio
import itertools
import os
import time
from collections.abc import Callable, Coroutine, Iterator
from types import ModuleType
from typing import TYPE_CHECKING, Any, TypeVar

import pytest

if TYPE_CHECKING:
    from aiogram import Bot, Dispatcher

_T = TypeVar("_T")

# Settings are read at import time and lru_cached — set the environment before
# anything from cb_* is imported.
os.environ.setdefault("CB_ENV", "test")
os.environ.setdefault("CB_TRACES_ENABLED", "false")
os.environ.setdefault("CB_LOG_JSON", "false")
os.environ.setdefault("CB_LOG_LEVEL", "WARNING")
os.environ.setdefault("CB_WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("CB_WEBHOOK_BASE_URL", "")
os.environ.setdefault("CB_BOT_TOKENS", '{"cookiebot": "424242:TEST"}')
# util_config.feature:13 requires the anonymous-mode tutorial video on a denial.
# Production sends it only when a real file_id is configured; the suite configures
# a fake one so the scenario exercises the behaviour rather than the empty branch.
os.environ.setdefault("CB_ANONYMOUS_TUTORIAL_FILE_ID", "tutorial-video-file-id")

# Imported after the environment block on purpose: Settings is read at import
# time and lru_cached, so anything importing cb_* earlier would freeze the wrong
# configuration for the whole session.
from qa.mock_telegram import MockTelegram  # noqa: E402
from qa.sandbox_harness import SandboxTelegram, mirror_inbound_update, sandbox_enabled  # noqa: E402

#: Either fake speaks the same public surface (`calls_to`, `admins`,
#: `set_admins`, `fail`, `clear_failures`, `reset`, `base_url`) — see
#: qa/sandbox_harness.py's module docstring for why swapping one for the
#: other needs no change to any step file.
TelegramFake = MockTelegram | SandboxTelegram

TEST_TOKEN = "424242:TEST"
BOT_USERNAME = "CookieMWbot"
GROUP_ID = -1001234567890
USER_ID = 555001
ADMIN_ID = 555100
NEWCOMER_ID = USER_ID + 1
# Telegram's fixed id for messages sent by an admin with anonymous mode on.
ANONYMOUS_BOT_ID = 1087968824


@pytest.fixture(scope="session")
def loop() -> Iterator[asyncio.AbstractEventLoop]:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield loop
    loop.close()


@pytest.fixture(scope="session")
def run(loop: asyncio.AbstractEventLoop) -> Callable[[Coroutine[Any, Any, Any]], Any]:
    def _run(coro: Coroutine[Any, Any, _T]) -> _T:
        return loop.run_until_complete(coro)

    return _run


@pytest.fixture(scope="session")
def telegram(run: Callable[[Coroutine[Any, Any, Any]], Any]) -> Iterator[TelegramFake]:
    """The suite's one Telegram fake — `MockTelegram` by default, or, with
    `CB_QA_SANDBOX=1`, `tg_sandbox.app:app` itself served on a real loopback
    port (`qa/sandbox_harness.py`). Either way `CB_TELEGRAM_API_BASE` ends up
    pointing at whichever is listening, which is the only thing the rest of
    the fixtures (`bot`, `dispatcher`) need to know.
    """
    fake: TelegramFake = SandboxTelegram() if sandbox_enabled() else MockTelegram()
    run(fake.start())
    os.environ["CB_TELEGRAM_API_BASE"] = fake.base_url
    yield fake
    run(fake.stop())


@pytest.fixture(scope="session")
def bot(telegram: TelegramFake) -> Bot:
    """A real aiogram Bot whose session points at the mock API."""
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.client.session.aiohttp import AiohttpSession
    from aiogram.client.telegram import TelegramAPIServer
    from aiogram.enums import ParseMode

    return Bot(
        token=TEST_TOKEN,
        session=AiohttpSession(api=TelegramAPIServer.from_base(telegram.base_url)),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


@pytest.fixture(scope="session")
def dispatcher(telegram: TelegramFake) -> Dispatcher:
    """The dispatcher cb-gateway actually serves — middlewares and routers included."""
    from cb_gateway.main import dp

    return dp


@pytest.fixture(scope="session")
def database(run: Callable[[Coroutine[Any, Any, Any]], Any]) -> Iterator[ModuleType]:
    """A real database for the scenarios whose feature genuinely reads or writes one.

    Most of the suite needs none: the dedupe middleware degrades to its in-process
    LRU, the event recorder buffers in memory, and `group_config` serves the v1
    defaults. But `/rules` answers *with* stored content — a handler that invented
    a reply when the database was unreachable would be worse than one that stays
    quiet, so those scenarios get the real thing (AGENTS.md §6: acceptance is
    "mock Telegram, sometimes DB").

    Skips rather than fails when nothing is listening, so `cb.py test` stays
    offline-friendly exactly like the integration layer.
    """
    from cb_core import db
    from cb_core.settings import Settings

    dsn = os.environ.get(
        "CB_TEST_PG_DSN",
        os.environ.get("CB_PG_DSN", "postgresql://cookiebot:cookiebot@localhost:5432/cookiebot"),
    )
    settings = Settings(
        pg_dsn=dsn,
        service_name="cb-qa",
        traces_enabled=False,
        # Same reason as qa/integration/conftest.py: the first array-typed statement
        # on a fresh connection triggers asyncpg's recursive catalog introspection,
        # which is seconds long against a Citus catalog on an emulated container.
        # At the 10s production default it expires mid-handler, and the scenario
        # fails as "the bot said nothing" rather than as a timeout.
        pg_command_timeout=float(os.environ.get("CB_TEST_PG_COMMAND_TIMEOUT", "60")),
    )
    try:
        run(db.init_pool(settings))
        if not run(db.healthcheck()):
            pytest.skip(f"database at {dsn} is not answering")
    except Exception as exc:  # noqa: BLE001 - any connection failure means "skip"
        pytest.skip(f"no database at {dsn}: {exc}")

    run(_seed_qa_group())
    yield db
    run(_drop_qa_group())
    run(db.close_pool())


async def _seed_qa_group() -> None:
    """The group every scenario talks in, reset to stock settings.

    The config row is *replaced*, not left alone: the /config scenarios really do
    flip `functions_fun`, `doomlist_enabled` and friends on this group, and a
    suite that ran afterwards then inherited a bot with features switched off.
    That produced failures nowhere near the scenario that caused them.
    """
    from cb_core import db

    await db.execute(
        """
        INSERT INTO groups (group_id, title, chat_type, skin)
        VALUES ($1, 'QA Group', 'supergroup', 'cookiebot')
        ON CONFLICT (group_id) DO NOTHING
        """,
        GROUP_ID,
        name="qa_seed_group",
    )
    await db.execute("DELETE FROM group_configs WHERE group_id = $1", GROUP_ID, name="qa_reset_cfg")
    await db.execute(
        "INSERT INTO group_configs (group_id) VALUES ($1)", GROUP_ID, name="qa_seed_config"
    )


async def _drop_qa_group() -> None:
    from cb_core import db

    await db.execute("DELETE FROM groups WHERE group_id = $1", GROUP_ID, name="qa_drop_group")


@pytest.fixture(scope="session")
def valkey(run: Callable[[Coroutine[Any, Any, Any]], Any]) -> Iterator[ModuleType]:
    """A real Valkey for scenarios whose behaviour *is* the shared counter.

    Faking `cb_core.cache.incr_window` would fake our own atomicity: the whole
    point of that function is that INCR and EXPIRE happen in one pipeline across
    replicas, and a dict stand-in proves nothing about it. Valkey itself is the
    outside world, so we run the real one.

    Uses database index 15 by default, never the dev index 0, because the
    per-scenario cleaner flushes it.
    """
    from cb_core import cache
    from cb_core.settings import Settings

    dsn = os.environ.get("CB_TEST_REDIS_DSN", "redis://localhost:6379/15")
    try:
        run(cache.init_cache(Settings(redis_dsn=dsn, service_name="cb-qa", traces_enabled=False)))
        if not run(cache.healthcheck()):
            pytest.skip(f"valkey at {dsn} is not answering")
    except Exception as exc:  # noqa: BLE001 - any connection failure means "skip"
        pytest.skip(f"no valkey at {dsn}: {exc}")

    yield cache
    run(cache.close_cache())


@pytest.fixture
def clean_cache(
    valkey: ModuleType, run: Callable[[Coroutine[Any, Any, Any]], Any]
) -> Iterator[None]:
    """Empty the test cache database around a scenario."""
    run(valkey.client().flushdb())
    yield
    run(valkey.client().flushdb())


def _table_cleaner(
    table: str,
) -> Callable[[ModuleType, Callable[[Coroutine[Any, Any, Any]], Any]], Iterator[None]]:
    """Per-scenario isolation for a table acceptance scenarios write to."""

    @pytest.fixture
    def _fixture(
        database: ModuleType, run: Callable[[Coroutine[Any, Any, Any]], Any]
    ) -> Iterator[None]:
        from cb_core import db

        stmt = f"DELETE FROM {table} WHERE group_id = $1"
        run(db.execute(stmt, GROUP_ID, name=f"qa_clean_{table}"))
        yield
        run(db.execute(stmt, GROUP_ID, name=f"qa_clean_{table}"))

    return _fixture


clean_rules = _table_cleaner("group_rules")
clean_welcomes = _table_cleaner("group_welcomes")
clean_captcha = _table_cleaner("captcha_challenges")
clean_members = _table_cleaner("group_members")


@pytest.fixture(autouse=True)
def _clean(
    telegram: TelegramFake, run: Callable[[Coroutine[Any, Any, Any]], Any]
) -> Iterator[None]:
    telegram.reset()
    telegram.clear_failures()
    # Admin-gated scenarios opt in with `telegram.set_admins(...)`. Starting from
    # "nobody is an admin" means a handler that forgets its check fails loudly
    # instead of passing because the harness was permissive.
    telegram.admins.clear()
    telegram.set_admins(GROUP_ID, [(ADMIN_ID, "administrator")])

    # Both caches are process-global with a TTL measured in seconds, which is far
    # longer than a scenario. Left alone, a scenario that made someone an admin
    # leaks that into the next scenario's "and now a non-admin tries it" — which
    # then passes while asserting the opposite of what it reads.
    from cb_core import admins, group_config

    admins._l1.clear()  # noqa: SLF001 - the L1 dict is the seam the harness owns
    group_config._l1.clear()  # noqa: SLF001 - same

    # L2 as well, when a suite has brought Valkey up: admin sets live there for
    # `admin_cache_seconds` (600) and group config for `config_cache_l2_seconds`
    # (900), both far longer than a scenario. Clearing only L1 left the previous
    # scenario's answer sitting in Valkey, so "a non-admin tries it" quietly read
    # back the admin set the scenario before it had installed.
    from cb_core import cache

    try:
        client = cache.client()
    except RuntimeError:
        pass  # no cache in this run
    else:
        run(client.flushdb())

    # Re-seed per scenario, not once per session. Handlers write `group_configs`
    # and `group_members`, both of which have a foreign key to `groups`, and the
    # QA group's row is only created by the `database` fixture — which a suite
    # that never asks for a database does not trigger. Whether the row exists
    # then depends on which suites ran first, which surfaced as foreign-key
    # violations in some orderings and green in others.
    from cb_core import db

    try:
        db.pool()
    except RuntimeError:
        pass  # no database in this run; nothing writes rows either
    else:
        run(_seed_qa_group())
    yield


# Distinct per process, monotonic, and never restarted per scenario.
#
# The dispatcher under test is session-scoped and carries the real dedupe
# middleware, so an update_id an earlier scenario already used is dropped as a
# Telegram redelivery — correct behaviour, and it surfaces as the mystifying
# "the bot said nothing" rather than as a duplicate.
#
# The step is 10_000, not 1, because several suites take one id and then walk it
# (`ctx.update_id += 1`) for the rest of the scenario. With a step of 1 those
# hand-rolled increments march straight into ids this counter hands out later,
# and the collision only bites when suites run in a particular order — which is
# exactly the kind of failure nobody debugs from the symptom.
_update_ids = itertools.count(int(time.time() * 1000) % 1_000_000_000, 10_000)


def next_update_id() -> int:
    return next(_update_ids)


class Context:
    """Per-scenario state shared between steps."""

    def __init__(self) -> None:
        self.bot_running: bool = True
        self.update_id: int = next_update_id()


@pytest.fixture
def ctx() -> Context:
    return Context()


def _user(user_id: int = USER_ID, name: str = "Tester", username: str = "tester") -> dict[str, Any]:
    return {"id": user_id, "is_bot": False, "first_name": name, "username": username}


def _chat(chat_id: int = GROUP_ID) -> dict[str, Any]:
    return {"id": chat_id, "type": "supergroup", "title": "QA Group"}


def make_message_update(
    text: str | None,
    update_id: int,
    *,
    user_id: int = USER_ID,
    chat_id: int = GROUP_ID,
    reply_to: dict[str, Any] | None = None,
    sticker: str | None = None,
    photo: bool = False,
    video: bool = False,
    animation: bool = False,
    anonymous: bool = False,
) -> dict[str, Any]:
    """One message update.

    The keyword arguments exist because M1 needs shapes M0 never sent: stickers
    (sticker spam), media (media restriction), replies (/newrules, /newwelcome)
    and anonymous admin posts, where Telegram replaces the sender with the
    GroupAnonymousBot and sets `sender_chat` to the group itself.
    """
    message: dict[str, Any] = {
        "message_id": update_id,
        "date": int(time.time()),
        "chat": _chat(chat_id),
        "from": _user(user_id),
    }
    if anonymous:
        message["from"] = {
            "id": ANONYMOUS_BOT_ID,
            "is_bot": True,
            "first_name": "Group",
            "username": "GroupAnonymousBot",
        }
        message["sender_chat"] = _chat(chat_id)
    if text is not None:
        message["text"] = text
        message["entities"] = (
            [{"offset": 0, "length": len(text.split(" ")[0]), "type": "bot_command"}]
            if text.startswith("/")
            else []
        )
    if reply_to is not None:
        message["reply_to_message"] = reply_to
    if sticker is not None:
        message["sticker"] = {
            "file_id": f"sticker-{sticker}",
            "file_unique_id": f"u-{sticker}",
            "width": 512,
            "height": 512,
            "is_animated": False,
            "is_video": False,
            "type": "regular",
            "set_name": sticker,
        }
    if photo:
        message["photo"] = [
            {
                "file_id": "photo-1",
                "file_unique_id": "up1",
                "width": 90,
                "height": 90,
                "file_size": 1,
            }
        ]
    if video:
        message["video"] = {
            "file_id": "video-1",
            "file_unique_id": "uv1",
            "width": 320,
            "height": 240,
            "duration": 3,
        }
    if animation:
        message["animation"] = {
            "file_id": "gif-1",
            "file_unique_id": "ug1",
            "width": 320,
            "height": 240,
            "duration": 3,
        }
    return {"update_id": update_id, "message": message}


def make_join_update(
    update_id: int,
    *,
    joiners: list[tuple[int, str]] | None = None,
    chat_id: int = GROUP_ID,
    by_user_id: int = USER_ID,
) -> dict[str, Any]:
    """A `new_chat_members` service message — welcome, captcha, doomlist, media restrict."""
    members = joiners or [(USER_ID + 1, "Newcomer")]
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": int(time.time()),
            "chat": _chat(chat_id),
            "from": _user(by_user_id),
            "new_chat_members": [_user(uid, name, username=name.lower()) for uid, name in members],
        },
    }


def make_leave_update(
    update_id: int, *, user_id: int = USER_ID + 1, chat_id: int = GROUP_ID
) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": int(time.time()),
            "chat": _chat(chat_id),
            "from": _user(user_id),
            "left_chat_member": _user(user_id, "Newcomer", username="newcomer"),
        },
    }


def make_callback_update(
    data: str,
    update_id: int,
    *,
    user_id: int = USER_ID,
    chat_id: int = GROUP_ID,
    message_id: int | None = None,
) -> dict[str, Any]:
    """An inline-keyboard press — the /config menu and the captcha button."""
    return {
        "update_id": update_id,
        "callback_query": {
            "id": str(update_id),
            "from": _user(user_id),
            "chat_instance": str(chat_id),
            "data": data,
            "message": {
                "message_id": message_id or update_id,
                "date": int(time.time()),
                "chat": _chat(chat_id),
                "from": {
                    "id": 424242,
                    "is_bot": True,
                    "first_name": "Cookiebot",
                    "username": BOT_USERNAME,
                },
                "text": "menu",
            },
        },
    }


def feed(
    run: Callable[[Coroutine[Any, Any, Any]], Any],
    dispatcher: Dispatcher,
    bot: Bot,
    payload: dict[str, Any],
) -> None:
    from aiogram.types import Update

    # A no-op unless CB_QA_SANDBOX=1: `feed()` hands `payload` straight to the
    # dispatcher, the same as it always has, skipping tg_sandbox's own
    # `/api/...` surface entirely — so the sandbox store never learns about
    # the *inbound* half of a scenario (the user's message, a join, a leave)
    # unless something tells it. This does exactly that.
    mirror_inbound_update(payload)

    update = Update.model_validate(payload, context={"bot": bot})
    run(dispatcher.feed_update(bot, update, skin="cookiebot", bot_username=BOT_USERNAME))
