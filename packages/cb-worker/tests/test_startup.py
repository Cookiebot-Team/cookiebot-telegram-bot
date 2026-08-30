"""Unit tests for the worker's own bot (design R3): built in `on_startup`,
closed in `on_shutdown`, resolving the same Telegram endpoint the gateway does.

HANDOFF gap 1 (captcha timeout cannot kick) and gap 5 (no gateway->worker
enqueue) both stalled on the worker having no way to talk to Telegram at all.
This only covers that piece — construction and endpoint selection — not any
job that uses `ctx["bot"]`.

Postgres, Valkey and blob storage are monkeypatched to no-ops: `startup`
touches all three, but none of them is what this file is about, and hitting
them for real would turn a unit test into an integration one.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from cb_core import tenancy
from cb_core.settings import Settings
from cb_worker import main


@pytest.fixture(autouse=True)
def _stub_infra(monkeypatch: pytest.MonkeyPatch) -> None:
    # pydantic-settings merges dict-typed fields (bot_tokens) across sources
    # instead of letting an explicit kwarg win outright, so a `CB_BOT_TOKENS`
    # left in the process environment leaks into every `_settings(...)` built
    # below. qa/conftest.py sets one (deliberately, via `setdefault`, for its
    # own suite) that survives for the rest of the pytest process once the
    # full tree is collected together (`testpaths = ["qa", "packages"]`), so
    # this file cannot assume a clean environment — strip every `CB_`-prefixed
    # var before each test builds its own `Settings`.
    for key in [k for k in os.environ if k.startswith("CB_")]:
        monkeypatch.delenv(key, raising=False)

    async def _none(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(main, "ensure_schema", _none)
    monkeypatch.setattr(main.db, "init_pool", _none)
    monkeypatch.setattr(main.db, "close_pool", _none)
    monkeypatch.setattr(main.cache, "init_cache", _none)
    monkeypatch.setattr(main.cache, "close_cache", _none)
    monkeypatch.setattr(main.storage, "init_storage", _none)
    monkeypatch.setattr(main.storage, "close_storage", _none)
    monkeypatch.setattr(main.storage, "store", lambda: type("S", (), {"scheme": "memory"})())
    monkeypatch.setattr(main.metrics, "start_metrics_server", lambda *a, **k: None)


def _settings(**kw: Any) -> Settings:
    defaults: dict[str, Any] = {
        "traces_enabled": False,
        "bot_tokens": {"cookiebot": "123:cookie-token"},
        "telegram_api_base": "",
        "telegram_api_local": False,
        "telegram_api_file_base": "",
        "telegram_files_root": "",
    }
    # `_env_file=None` for the same reason the fixture above strips `CB_*`, and
    # for the half it cannot reach: `.env` is a *file* source, so a developer
    # who has one (CONTRIBUTING says to make one, and `scripts/qa_setup.py`
    # writes one) gets its `CB_BOT_TOKENS` merged into the dict passed here and
    # these assertions fail on their machine and nowhere else.
    return Settings(**{**defaults, **kw}, _env_file=None)


class TestPrimaryToken:
    def test_prefers_the_default_tenant(self) -> None:
        settings = _settings(bot_tokens={"bombot": "456:bom", "cookiebot": "123:cookie"})
        assert main._primary_token(settings) == "123:cookie"  # noqa: SLF001 - testing the seam directly
        assert tenancy.DEFAULT_TENANT in settings.bot_tokens

    def test_falls_back_to_whatever_is_configured(self) -> None:
        settings = _settings(bot_tokens={"bombot": "456:bom"})
        assert main._primary_token(settings) == "456:bom"  # noqa: SLF001

    def test_empty_bot_tokens_yields_empty_token(self) -> None:
        settings = _settings(bot_tokens={})
        assert main._primary_token(settings) == ""  # noqa: SLF001


class TestStartupShutdown:
    async def test_bot_uses_cloud_api_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(main, "settings", _settings())
        ctx: dict[str, Any] = {}
        await main.startup(ctx)
        try:
            bot = ctx["bot"]
            assert bot.session.api.base == "https://api.telegram.org/bot{token}/{method}"
        finally:
            await main.shutdown(ctx)

    async def test_bot_follows_the_self_hosted_base_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole point of R3: a self-hosted `telegram-bot-api` base URL set
        for the gateway must be honoured by the worker too, or the worker's
        DMs/kicks would silently go to api.telegram.org instead."""
        monkeypatch.setattr(
            main,
            "settings",
            _settings(telegram_api_base="http://telegram-bot-api:8081", telegram_api_local=True),
        )
        ctx: dict[str, Any] = {}
        await main.startup(ctx)
        try:
            bot = ctx["bot"]
            api = bot.session.api
            assert api.is_local is True
            assert api.base.startswith("http://telegram-bot-api:8081/bot")
        finally:
            await main.shutdown(ctx)

    async def test_shutdown_closes_the_bot_session(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(main, "settings", _settings())
        ctx: dict[str, Any] = {}
        await main.startup(ctx)
        bot = ctx["bot"]
        # nothing sent yet, nothing to close
        assert bot.session._session is None  # noqa: SLF001 - asserting the lazy-open seam

        await main.shutdown(ctx)
        # No new session should have been opened by closing.
        assert bot.session._session is None  # noqa: SLF001

    async def test_shutdown_without_a_bot_does_not_raise(self) -> None:
        await main.shutdown({})
