"""How updates get into the dispatcher.

The handler stack does not care where an update came from, so the transport is
swappable. Three modes:

* **webhook** — production. Telegram POSTs to `/webhook/{skin}`; the FastAPI route
  feeds the dispatcher directly, so there is nothing to start here.
* **polling** — development, and self-hosted Bot API servers with no public URL.
  One long-poll task per skin, running inside the same process.
* **websocket** — reserved. Not a Telegram transport: it is the seam for a future
  operator/console channel and for tenant-supplied bots that push updates to us
  over a persistent connection instead of us polling them. Declaring it here
  (rather than bolting it on later) is what keeps `main.py` from growing a second
  update path. See docs/MULTI-TENANT.md.

Everything funnels through `Dispatcher.feed_update`, so dedupe, telemetry and the
analytics row happen identically regardless of transport.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Protocol

from aiogram import Bot, Dispatcher

from cb_core.logging import get_logger
from cb_core.settings import Settings
from cb_gateway.bots import BotRegistry

log = get_logger("cb.ingest")


class Ingest(Protocol):
    """A source of Telegram updates."""

    @property
    def mode(self) -> str: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...


class WebhookIngest:
    """Registers webhooks; the HTTP route does the feeding."""

    def __init__(self, registry: BotRegistry, dispatcher: Dispatcher, settings: Settings) -> None:
        self._registry = registry
        self._dp = dispatcher
        self._settings = settings

    @property
    def mode(self) -> str:
        return "webhook"

    async def start(self) -> None:
        base = self._settings.webhook_base_url.rstrip("/")
        if not base:
            log.info("webhook.skipped", reason="CB_WEBHOOK_BASE_URL empty")
            return
        for skin, bot in self._registry.items():
            await bot.set_webhook(
                url=f"{base}/webhook/{skin}",
                secret_token=self._settings.webhook_secret or None,
                drop_pending_updates=False,
                allowed_updates=self._dp.resolve_used_update_types(),
            )
            log.info("webhook.set", skin=skin)

    async def stop(self) -> None:
        return None


class PollingIngest:
    """One long-poll task per skin.

    Unlike v1 — five OS processes each long-polling with a 50-thread pool and a
    supervisor that killed the process over 70% host CPU — these are asyncio tasks
    in one process, cancelled cleanly on shutdown.
    """

    def __init__(self, registry: BotRegistry, dispatcher: Dispatcher, settings: Settings) -> None:
        self._registry = registry
        self._dp = dispatcher
        self._settings = settings
        self._tasks: list[asyncio.Task[None]] = []

    @property
    def mode(self) -> str:
        return "polling"

    async def start(self) -> None:
        for skin, bot in self._registry.items():
            # A webhook and long polling are mutually exclusive per bot.
            await bot.delete_webhook(drop_pending_updates=False)
            self._tasks.append(asyncio.create_task(self._poll(skin, bot), name=f"cb-poll-{skin}"))
        log.info("polling.started", skins=self._registry.skins())

    async def _poll(self, skin: str, bot: Bot) -> None:
        try:
            await self._dp.start_polling(
                bot,
                polling_timeout=self._settings.telegram_polling_timeout,
                handle_signals=False,
                skin=skin,
                bot_username=self._registry.username(skin),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("polling.failed", skin=skin, error=str(exc))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            # Cancellation is the expected path; a poller that died of its own
            # error already logged it. Either way, shutdown continues.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()
        log.info("polling.stopped")


class WebsocketIngest:
    """Placeholder for the persistent-connection transport.

    Deliberately fails loudly rather than silently doing nothing: an operator who
    sets `CB_TELEGRAM_INGEST=websocket` today should get an error, not a bot that
    starts and never receives an update.
    """

    def __init__(self, *_: object, **__: object) -> None:
        return None

    @property
    def mode(self) -> str:
        return "websocket"

    async def start(self) -> None:
        raise NotImplementedError(
            "websocket ingest is not implemented yet — see docs/MULTI-TENANT.md; "
            "use CB_TELEGRAM_INGEST=webhook or polling"
        )

    async def stop(self) -> None:
        return None


def build_ingest(registry: BotRegistry, dispatcher: Dispatcher, settings: Settings) -> Ingest:
    match settings.telegram_ingest:
        case "polling":
            return PollingIngest(registry, dispatcher, settings)
        case "websocket":
            return WebsocketIngest()
        case _:
            return WebhookIngest(registry, dispatcher, settings)
