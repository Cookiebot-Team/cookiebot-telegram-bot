"""Bot registry and Telegram API endpoint selection.

`core_botskins` in v1 meant five separate OS processes, each with its own token,
its own 50-thread pool and its own divergent in-memory caches — the skin was a
CLI argument (`COOKIEBOT.py:24-32`). Here one process serves every skin; the skin
is a lookup, and adding one is a row plus an env entry. That is also the seam the
multi-tenant plan builds on (docs/site/content/docs/multi-tenant.mdx).

Three endpoint modes, chosen by configuration:

* **cloud** — `api.telegram.org` (default).
* **self-hosted** — a local `telegram-bot-api` server. Lifts the 20 MB download /
  50 MB upload caps to 2 GB, removes the per-bot rate limits, and allows a webhook
  pointed at a private address. In *local mode* the server writes files straight
  to disk and `getFile` returns an absolute path rather than a download URL, so
  file handling changes — aiogram is told about it via `is_local`.
* **mock** — the in-process fake used by the acceptance suite.
"""

from __future__ import annotations

from pathlib import Path

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import (
    BareFilesPathWrapper,
    FilesPathWrapper,
    SimpleFilesPathWrapper,
    TelegramAPIServer,
)
from aiogram.enums import ParseMode

from cb_core.logging import get_logger
from cb_core.settings import Settings
from cb_gateway.telemetry import BotAPIRequestTracing

log = get_logger("cb.bots")


def build_api_server(settings: Settings) -> TelegramAPIServer | None:
    """The API endpoint for every bot in this process, or None for the cloud API."""
    if not settings.telegram_api_base:
        return None

    base = settings.telegram_api_base.rstrip("/")
    file_base = settings.telegram_api_file_base.rstrip("/") or base

    if settings.telegram_api_local:
        # Local mode: getFile returns a path on the server's filesystem. When that
        # filesystem is mounted elsewhere in this container, telegram_files_root
        # rewrites the prefix so aiogram opens the right path.
        return TelegramAPIServer(
            base=f"{base}/bot{{token}}/{{method}}",
            file=f"{file_base}/file/bot{{token}}/{{path}}",
            is_local=True,
            wrap_local_file=_local_file_wrapper(settings),
        )

    return TelegramAPIServer.from_base(base)


# Where the self-hosted server writes files inside its own container.
_SERVER_FILES_ROOT = "/var/lib/telegram-bot-api"


def _local_file_wrapper(settings: Settings) -> FilesPathWrapper:
    """Map the server's path onto the path this process sees.

    Identical filesystems (same host, same mount) need no translation; separate
    containers sharing a volume do.
    """
    root = settings.telegram_files_root.rstrip("/")
    if not root:
        return BareFilesPathWrapper()
    return SimpleFilesPathWrapper(Path(_SERVER_FILES_ROOT), Path(root))


class BotRegistry:
    """skin -> Bot. Also resolves a bot by the token embedded in the webhook path."""

    def __init__(self) -> None:
        self._by_skin: dict[str, Bot] = {}
        self._usernames: dict[str, str] = {}
        self._api: TelegramAPIServer | None = None

    async def load(self, settings: Settings) -> None:
        self._api = build_api_server(settings)
        for skin, token in settings.bot_tokens.items():
            bot = Bot(
                token=token,
                session=AiohttpSession(api=self._api) if self._api else AiohttpSession(),
                default=DefaultBotProperties(parse_mode=ParseMode.HTML),
            )
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
