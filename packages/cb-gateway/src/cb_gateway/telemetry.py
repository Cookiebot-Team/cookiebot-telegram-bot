"""Gateway-only telemetry: outcome tagging on the per-update root span, and a
span + metric for every outbound Bot API call.

`TelemetryMiddleware` (`middlewares.py`) opens one root span per update, named
after the resolved command/interaction. That span can say "the handler ran" and
"the handler raised" for free, but this bot's whole design relies on handlers
that *decline* to reply (a feature switched off, an admin check that fails) or
that stay quiet on purpose (`cb_gateway/context.py`'s `deny_if_disabled` is the
"decline and say so" half; a bare `return` with a comment explaining why is the
"say nothing" half). Without `mark_outcome`, a trace cannot tell either of those
apart from "the reply just didn't render" — see AGENTS.md §7 and the module
docstring in `middlewares.py`.

The Bot API side exists because `cb_core.telemetry.setup_tracing` only
auto-instruments asyncpg/httpx/redis (the libraries those services' own IO goes
through); aiogram talks to Telegram over its own aiohttp session, which none of
those cover. Without this, a trace shows a handler's own work but not the one
round trip that is usually the actual latency — and `cb_telegram_api_duration_seconds`
(`cb_core/metrics.py`) was defined but never observed anywhere.
"""

from __future__ import annotations

import time
from typing import Literal

from aiogram import Bot
from aiogram.client.session.middlewares.base import (
    BaseRequestMiddleware,
    NextRequestMiddlewareType,
)
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.methods import TelegramMethod
from aiogram.methods.base import Response, TelegramType
from opentelemetry import trace
from opentelemetry.trace import SpanKind

from cb_core import metrics
from cb_core.telemetry import span

# The only three shapes worth telling apart on a root span: a full answer, a
# deliberate decline that still told the user, and deliberate silence. A crash
# is already distinguishable — it is the span with an exception event and no
# `cb.outcome` override, since `TelemetryMiddleware`'s `except` block sets this
# to "error" itself.
Outcome = Literal["answered", "refused", "silent"]

OUTCOME_ATTR = "cb.outcome"


def mark_outcome(outcome: Outcome) -> None:
    """Call at the exact point a handler decides to refuse or stay silent — the
    same line that already carries a comment explaining why, per AGENTS.md §7.

    A no-op if no span is open (tracing disabled, or called outside the update
    lifecycle): `get_current_span()` then returns a `NonRecordingSpan` whose
    `set_attribute` does nothing, so this is always safe to call.
    """
    trace.get_current_span().set_attribute(OUTCOME_ATTR, outcome)


class BotAPIRequestTracing(BaseRequestMiddleware):
    """Registered on every `Bot.session` in `BotRegistry.load` (`bots.py`).

    A 429 is not folded into the generic "error" outcome: `telegram_rate_limited_total`
    already exists for it (previously unused — this is the first thing that
    increments it) and a rate limit is an operational signal a plain exception
    count would bury.
    """

    async def __call__(
        self,
        make_request: NextRequestMiddlewareType[TelegramType],
        bot: Bot,
        method: TelegramMethod[TelegramType],
    ) -> Response[TelegramType]:
        name = type(method).__api_method__
        start = time.perf_counter()
        outcome = "ok"
        with span(f"telegram.api.{name}", kind=SpanKind.CLIENT, **{"telegram.method": name}):
            try:
                return await make_request(bot, method)
            except TelegramRetryAfter:
                outcome = "rate_limited"
                metrics.telegram_rate_limited_total.labels(method=name).inc()
                raise
            except TelegramAPIError:
                # Business failures Telegram itself rejected (chat not found, bot
                # blocked...) — the span's exception event comes for free from
                # `start_as_current_span`'s default recording, same as
                # `cb_core.llm.router.complete`'s span relies on for its own
                # re-raised exceptions.
                outcome = "error"
                raise
            finally:
                metrics.telegram_api_duration.labels(method=name, outcome=outcome).observe(
                    time.perf_counter() - start
                )


__all__ = ["OUTCOME_ATTR", "BotAPIRequestTracing", "Outcome", "mark_outcome"]
