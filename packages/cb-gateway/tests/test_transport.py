"""Endpoint and ingest selection.

The self-hosted Bot API server changes two things that are easy to get silently
wrong: the URL templates, and `is_local` (which decides whether `getFile` yields
a download URL or a path on disk). Both are asserted here rather than discovered
in production.
"""

from __future__ import annotations

from typing import Any

import pytest
from aiogram import Dispatcher
from aiogram.client.telegram import BareFilesPathWrapper, SimpleFilesPathWrapper

from cb_core.settings import Settings
from cb_gateway.bots import BotRegistry, build_api_server
from cb_gateway.ingest import PollingIngest, WebhookIngest, WebsocketIngest, build_ingest


def _settings(**kw: Any) -> Settings:
    # Pin every field this module reads. Settings also reads the environment, and
    # the acceptance suite exports CB_TELEGRAM_API_BASE for its mock server — a
    # unit test must not inherit that.
    defaults = {
        "traces_enabled": False,
        "telegram_api_base": "",
        "telegram_api_local": False,
        "telegram_api_file_base": "",
        "telegram_files_root": "",
        "telegram_ingest": "webhook",
    }
    return Settings(**{**defaults, **kw})


class TestEndpointSelection:
    def test_cloud_by_default(self) -> None:
        assert build_api_server(_settings()) is None

    def test_custom_base_without_local_mode(self) -> None:
        api = build_api_server(_settings(telegram_api_base="http://mock:9000"))
        assert api is not None
        assert api.is_local is False
        assert "http://mock:9000" in api.base

    def test_self_hosted_sets_local_mode(self) -> None:
        api = build_api_server(
            _settings(telegram_api_base="http://localhost:8082", telegram_api_local=True)
        )
        assert api is not None
        assert api.is_local is True
        assert api.base.startswith("http://localhost:8082/bot")
        assert "{token}" in api.base and "{method}" in api.base
        assert "{path}" in api.file

    def test_trailing_slash_does_not_double_up(self) -> None:
        api = build_api_server(
            _settings(telegram_api_base="http://localhost:8082/", telegram_api_local=True)
        )
        assert api is not None
        assert "//bot" not in api.base

    def test_separate_file_base(self) -> None:
        api = build_api_server(
            _settings(
                telegram_api_base="http://api:8081",
                telegram_api_file_base="http://files:8081",
                telegram_api_local=True,
            )
        )
        assert api is not None
        assert api.file.startswith("http://files:8081/file/bot")

    def test_no_path_rewrite_when_filesystems_match(self) -> None:
        api = build_api_server(
            _settings(telegram_api_base="http://localhost:8082", telegram_api_local=True)
        )
        assert isinstance(api.wrap_local_file, BareFilesPathWrapper)

    def test_path_rewrite_when_volume_is_mounted_elsewhere(self) -> None:
        api = build_api_server(
            _settings(
                telegram_api_base="http://localhost:8082",
                telegram_api_local=True,
                telegram_files_root="/mnt/tg",
            )
        )
        assert isinstance(api.wrap_local_file, SimpleFilesPathWrapper)


class TestIngestSelection:
    @pytest.fixture
    def dispatcher(self) -> Dispatcher:
        return Dispatcher()

    def test_webhook_is_the_default(self, dispatcher: Dispatcher) -> None:
        ingest = build_ingest(BotRegistry(), dispatcher, _settings())
        assert isinstance(ingest, WebhookIngest)
        assert ingest.mode == "webhook"

    def test_polling_selected_by_config(self, dispatcher: Dispatcher) -> None:
        ingest = build_ingest(BotRegistry(), dispatcher, _settings(telegram_ingest="polling"))
        assert isinstance(ingest, PollingIngest)

    def test_websocket_is_reserved_and_fails_loudly(self, dispatcher: Dispatcher) -> None:
        """A reserved mode must error, not start a bot that receives nothing."""
        ingest = build_ingest(BotRegistry(), dispatcher, _settings(telegram_ingest="websocket"))
        assert isinstance(ingest, WebsocketIngest)

    async def test_websocket_start_raises(self, dispatcher: Dispatcher) -> None:
        ingest = build_ingest(BotRegistry(), dispatcher, _settings(telegram_ingest="websocket"))
        with pytest.raises(NotImplementedError, match="not implemented"):
            await ingest.start()

    def test_unknown_ingest_mode_is_rejected_at_config_time(self) -> None:
        with pytest.raises(ValueError, match="telegram_ingest"):
            _settings(telegram_ingest="carrier-pigeon")
