"""Bot registry: skin -> Bot, and webhook-path token resolution.

`core_botskins` in v1 meant five separate OS processes, each with its own token,
its own 50-thread pool and its own divergent in-memory caches — the skin was a
CLI argument (`COOKIEBOT.py:24-32`). Here one process serves every skin; the skin
is a lookup, and adding one is a row plus an env entry. That is also the seam the
multi-tenant plan builds on (docs/site/content/docs/multi-tenant.mdx).

Endpoint selection itself (`build_api_server`, `build_bot`) lives in
`cb_core.bot` — `cb-worker` needs to build a `Bot` against the same endpoint,
and a gateway-only helper would have been a second, driftable copy of that
logic (AGENTS.md §8). Re-exported here so existing imports keep working.
"""

from __future__ import annotations

from aiogram import Bot
from aiogram.client.telegram import TelegramAPIServer

from cb_core.bot import build_api_server, build_bot
from cb_core.logging import get_logger
from cb_core.settings import Settings
from cb_gateway.telemetry import BotAPIRequestTracing

log = get_logger("cb.bots")

__all__ = ["BotRegistry", "build_api_server", "registry"]


class BotRegistry:
    """skin -> Bot. Also resolves a bot by the token embedded in the webhook path."""

    def __init__(self) -> None:
        self._by_skin: dict[str, Bot] = {}
        self._usernames: dict[str, str] = {}
        self._api: TelegramAPIServer | None = None

    async def load(self, settings: Settings) -> None:
        self._api = build_api_server(settings)
        for skin, token in settings.bot_tokens.items():
            bot = build_bot(token, settings, self._api)
            # One span + one cb_telegram_api_duration_seconds observation per
            # outbound call — see cb_gateway/telemetry.py's module docstring for
            # why this cannot come from cb_core.telemetry's generic auto-instrumentation.
            bot.session.middleware.register(BotAPIRequestTracing())
            self._by_skin[skin] = bot
            log.info(
                "bot.registered",
                skin=skin,
                endpoint="self-hosted"
                if settings.telegram_api_local
                else ("custom" if self._api else "cloud"),
            )

    async def resolve_usernames(self) -> None:
        """Cached once at startup; needed to ignore /cmd@OtherBot in shared groups."""
        for skin, bot in self._by_skin.items():
            try:
                me = await bot.get_me()
                self._usernames[skin] = me.username or ""
                log.info("bot.identified", skin=skin, username=me.username)
            except Exception as exc:  # noqa: BLE001
                self._usernames[skin] = ""
                log.warning("bot.identify.failed", skin=skin, error=str(exc))

    def get(self, skin: str) -> Bot | None:
        return self._by_skin.get(skin)

    def username(self, skin: str) -> str:
        return self._usernames.get(skin, "")

    def skins(self) -> list[str]:
        return list(self._by_skin)

    def items(self) -> list[tuple[str, Bot]]:
        return list(self._by_skin.items())

    @property
    def api_server(self) -> TelegramAPIServer | None:
        return self._api

    async def close(self) -> None:
        for bot in self._by_skin.values():
            await bot.session.close()


registry = BotRegistry()
