"""Structured logging that does not bind this package to any host application.

Every call site in this package logs the way structlog wants — an event name
plus keyword fields (`log.warning("sandbox.db.connect_failed", error=...)`) —
because that is the shape a host application's own pipeline can consume
without reformatting. But the sandbox has to run as a standalone tool in a
repository that has never heard of structlog, so this module resolves the
logger at import time and falls back to the standard library.

The fallback is not a no-op: a workbench that swallows the one warning
explaining why its database is empty is worse than one that prints an ugly
line. It renders the same event name and the same fields, just flattened into
`stdlib`'s single message string.

If the host application configures structlog itself, that configuration
applies here for free — this module never calls `structlog.configure`, it only
asks for a logger.
"""

from __future__ import annotations

import logging
from typing import Any

try:  # pragma: no cover - exercised by whichever branch the environment has
    import structlog

    _HAVE_STRUCTLOG = True
except ImportError:  # pragma: no cover
    _HAVE_STRUCTLOG = False


class _StdlibLogger:
    """The structlog keyword-field API, rendered onto a stdlib logger.

    `logging` has no notion of event fields, so they are appended as
    `key=value` pairs after the event name. Deliberately `repr`-formatted:
    the fields this package logs are paths, error strings and ids, and a bare
    `str()` of a path that happens to contain a space is ambiguous in a way a
    log line meant for debugging cannot afford.
    """

    __slots__ = ("_log",)

    def __init__(self, name: str) -> None:
        self._log = logging.getLogger(name)

    @staticmethod
    def _render(event: str, fields: dict[str, Any]) -> str:
        if not fields:
            return event
        rendered = " ".join(f"{key}={value!r}" for key, value in fields.items())
        return f"{event} {rendered}"

    def debug(self, event: str, **fields: Any) -> None:
        self._log.debug(self._render(event, fields))

    def info(self, event: str, **fields: Any) -> None:
        self._log.info(self._render(event, fields))

    def warning(self, event: str, **fields: Any) -> None:
        self._log.warning(self._render(event, fields))

    def error(self, event: str, **fields: Any) -> None:
        self._log.error(self._render(event, fields))

    def exception(self, event: str, **fields: Any) -> None:
        self._log.exception(self._render(event, fields))


def get_logger(name: str) -> Any:
    """A logger with structlog's keyword-field API, whatever is installed.

    Returns `Any` rather than a protocol: structlog's own `BoundLogger` and
    `_StdlibLogger` share the call shape this package uses but not a common
    base, and inventing a Protocol for five methods that are only ever called,
    never passed around, would be more type machinery than it buys.
    """
    if _HAVE_STRUCTLOG:
        return structlog.get_logger(name)
    return _StdlibLogger(name)
