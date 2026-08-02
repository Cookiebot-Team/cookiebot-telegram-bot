"""Bot construction shared by every service that talks to Telegram.

Originally lived only in `cb_gateway.bots` (webhook ingest is the only thing
that used to need a `Bot`). `cb-worker` now needs one too — DMs and kicks are
fan-out work, which by design never runs on the gateway's reply path — and it
must resolve the same endpoint the gateway does. A worker that built its own
`Bot` independently could silently drift onto `api.telegram.org` while the
gateway talks to a self-hosted `telegram-bot-api` server; putting the
construction here, called by both, makes that impossible instead of merely
undesirable.

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

from cb_core.settings import Settings

# Where the self-hosted server writes files inside its own container.
_SERVER_FILES_ROOT = "/var/lib/telegram-bot-api"


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


def _local_file_wrapper(settings: Settings) -> FilesPathWrapper:
    """Map the server's path onto the path this process sees.

    Identical filesystems (same host, same mount) need no translation; separate
    containers sharing a volume do.
    """
    root = settings.telegram_files_root.rstrip("/")
    if not root:
        return BareFilesPathWrapper()
    return SimpleFilesPathWrapper(Path(_SERVER_FILES_ROOT), Path(root))


def build_bot(token: str, settings: Settings, api: TelegramAPIServer | None = None) -> Bot:
    """One `aiogram.Bot` for `token`, wired to whichever endpoint `settings` selects.

    `api` lets a caller building several bots against the same endpoint (the
    gateway's multi-skin `BotRegistry`) resolve `build_api_server` once and
    reuse it; omit it to have this resolve its own (a single-bot caller, like
    the worker).
    """
    resolved = api if api is not None else build_api_server(settings)
    return Bot(
        token=token,
        session=AiohttpSession(api=resolved) if resolved else AiohttpSession(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


__all__ = ["build_api_server", "build_bot"]
