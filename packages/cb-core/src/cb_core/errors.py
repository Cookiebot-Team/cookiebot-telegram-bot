"""Errors that say where they came from.

A traceback names the frames a failure passed through. It does not name the
*work* it was doing: which group, which column, which job, which method. That
gap is why the group_configs foreign-key failure read as `TelegramBadRequest`
in one place, `insert or update on table "group_configs_102052" violates …` in
another, and `error=""` in a third — three renderings of one interaction, none
of which said "writing language for group -5528379079".

So a failure is wrapped as it crosses each layer that knows something the layer
below did not:

    async with fail_as("group_config.set_config", group_id=group_id, columns=cols):
        await db.execute(stmt, ...)

The original exception is never replaced — it becomes `__cause__`, so the
traceback still ends where the failure actually happened. What is added is a
link in a chain, and the chain is what gets rendered:

    ConfigWriteError: group_config.set_config(group_id=-5528379079, columns=language)
      ← ForeignKeyViolationError: insert or update on table "group_configs_102052" …

`chain()` returns that as data (one dict per link) for structured logs and span
attributes; `render()` returns the arrow form above for a human; `reason()`
returns the one line a user in a chat should see, which is the *innermost*
failure — the outer links are context the operator wants and the user cannot
act on.

Nothing here catches anything. `fail_as` re-raises, always: an error that is
described but not propagated is worse than one that was never described.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

#: How deep a rendered chain goes before it is truncated. Chains longer than
#: this are a sign of over-wrapping, not of a deep failure, and a log line that
#: never ends is one nobody reads.
MAX_LINKS = 8

#: How much of an exception's own message survives into a rendered chain. Long
#: enough for a Postgres DETAIL line, short enough for a Telegram message.
MAX_MESSAGE = 300


class CbError(Exception):
    """A failure with the operation and the fields that identify it.

    Raised only by `fail_as`, and only ever `from` an original exception —
    there is no such thing as a `CbError` that is not wrapping something.
    """

    def __init__(self, operation: str, **context: object) -> None:
        self.operation = operation
        self.context: Mapping[str, object] = context
        super().__init__(_describe(operation, context))


def _describe(operation: str, context: Mapping[str, object]) -> str:
    if not context:
        return operation
    fields = ", ".join(f"{k}={v}" for k, v in context.items() if v is not None)
    return f"{operation}({fields})" if fields else operation


@contextmanager
def fail_as(operation: str, **context: object) -> Iterator[None]:
    """Re-raise whatever escapes this block as a `CbError` naming the work.

    `BaseException` that is not `Exception` — cancellation, shutdown — passes
    through untouched: a cancelled task is not a failure of the operation, and
    wrapping it would turn an orderly shutdown into a reported error.
    """
    try:
        yield
    except CbError:
        # Already described by an inner layer. Wrapping it again would add a
        # link that says nothing the next one does not.
        raise
    except Exception as exc:
        raise CbError(operation, **context) from exc


def chain(exc: BaseException | None) -> tuple[dict[str, Any], ...]:
    """The failure and everything it was caused by, outermost first.

    Follows `__cause__` (explicit `raise … from …`) and falls back to
    `__context__` (an exception raised while handling another), which is what
    catches the case nobody wrote a `from` for. Cycles are impossible to build
    with `raise from` but are guarded anyway — a rendering helper must not be
    the thing that hangs an error path.
    """
    links: list[dict[str, Any]] = []
    seen: set[int] = set()
    current = exc
    while current is not None and len(links) < MAX_LINKS:
        if id(current) in seen:
            break
        seen.add(id(current))
        link: dict[str, Any] = {
            "type": type(current).__name__,
            "message": _trim(str(current)),
        }
        if isinstance(current, CbError):
            link["operation"] = current.operation
            if current.context:
                link["context"] = {k: v for k, v in current.context.items() if v is not None}
        links.append(link)
        current = current.__cause__ or current.__context__
    return tuple(links)


def render(exc: BaseException | None, *, separator: str = " <- ") -> str:
    """The chain as one line: outermost first, innermost last.

    The innermost link is the one that actually failed; everything to its left
    is what was being attempted. Reading it right to left answers "what broke",
    reading it left to right answers "while doing what".
    """
    return separator.join(f"{link['type']}: {link['message']}" for link in chain(exc))


def root(exc: BaseException | None) -> BaseException | None:
    """The innermost cause — the thing that actually failed."""
    current = exc
    seen: set[int] = set()
    while current is not None:
        following = current.__cause__ or current.__context__
        if following is None or id(following) in seen:
            return current
        seen.add(id(current))
        current = following
    return None


def reason(exc: BaseException | None) -> str:
    """One line for the person in the chat.

    The innermost failure, not the outermost: "insert or update on table … "
    is something an admin can act on or paste to whoever can. "ConfigWriteError:
    group_config.set_config(...)" is the operator's half of the same fact and
    means nothing to them. The trace id in the same message is what joins the
    two.
    """
    innermost = root(exc)
    if innermost is None:
        return ""
    message = _trim(str(innermost), limit=200)
    return message or type(innermost).__name__


def _trim(message: str, *, limit: int = MAX_MESSAGE) -> str:
    collapsed = " ".join(message.split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


__all__ = ["CbError", "chain", "fail_as", "reason", "render", "root"]
