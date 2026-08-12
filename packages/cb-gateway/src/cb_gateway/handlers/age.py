"""x_age_guess — `/idade`, `/age`, `/edad`: guess a name's age via agify.io.

v1: `age`, `../COOKIEBOT-Telegram-Group-Bot/Bot/Miscellaneous.py:185-202`::

    def age(cookiebot, msg, chat_id, language):
        if not " " in msg['text']:
            text = i18n.get("age", lang=language)
            send_message(cookiebot, chat_id, text, msg, language)
        else:
            nome = msg['text'].replace("/idade ", '').replace("/edad ", '').replace("/age ", '') \\
                .replace("/idade@CookieMWbot", '').replace("/age@CookieMWbot", '') \\
                .replace("/edad@CookieMWbot", '').split()[0]
            response = json.loads(requests.get(f"https://api.agify.io?name={nome}", timeout=10).text)
            registered_times = response['count']
            if registered_times == 0:
                text = i18n.get("not_know", lang=language)
                send_message(cookiebot, chat_id, text, msg)
            else:
                ctx = {"age": response['age'], "registered_times": registered_times}
                text = i18n.get("age_yes", lang=language, **ctx)
                send_message(cookiebot, chat_id, text, msg)

Dispatched at `COOKIEBOT.py:226-227`, inside the `funfunctions`-gated block
whose `else` answers with the `fun_off` text (`:218-219`, `notify_fun_off`) —
so this handler checks the gate itself and replies, the pattern
`fun_random.py`/`unearth.py` established, rather than the silent
`FeatureGate` filter.

## Deviations from v1, and why

1. **Argument parsing.** v1 builds the name with a chain of `.replace()`
   calls covering every alias and `@CookieMWbot`, then `.split()[0]` — which
   raises an uncaught `IndexError` (answering nothing) for a message like
   "/age " (a lone trailing space): `" " in msg['text']` is true, so v1 takes
   the *else* branch, and the replace-chain leaves an empty string with
   nothing to split. v2's dispatcher already isolates the argument text
   (`ParsedCommand.args`, pre-stripped by `textmatch.parse_command`), so this
   reads `parsed.args` directly and treats an empty result as "no argument" —
   the same outcome v1's no-space branch produces, without the crash. The
   first whitespace-separated token of whatever remains is still the name,
   exactly as v1's own `.split()[0]`.
2. **URL encoding.** v1 splices `nome` into the URL with an f-string,
   unescaped. This passes it as an httpx query parameter
   (`params={"name": name}`), which percent-encodes it — a strictly safer
   request for the same query, not a behaviour change for any name that was
   ever valid input before.
3. **External failure.** v1 has no `try`/`except` around the `requests.get`
   call or the `json.loads` of its body: a timeout, a non-200, or a malformed
   response propagates out of `age()` entirely, to whichever bare
   `except Exception` sits above it in the dispatcher — which answers
   nothing. There is no v1 behaviour to preserve for "agify is down"; v1
   simply never handles it. This port answers with the same `not_know` text
   `count == 0` already uses: from the group's point of view "the service
   could not tell us" and "the service has no data for this name" are the
   same observable fact, and reusing the existing string avoids inventing a
   new catalog key for a case v1's own data never anticipated. A timeout, a
   non-2xx status, a malformed body, a missing/non-numeric `count` field, and
   an open breaker all take this path — see `_lookup`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, cast

import httpx
from aiogram import Bot, Router
from aiogram.types import Message

from cb_core import metrics
from cb_core.breaker import Breaker
from cb_core.logging import get_logger
from cb_core.textmatch import ParsedCommand
from cb_gateway.context import context_for, deny_if_disabled, t
from cb_gateway.filters import CommandName

log = get_logger("cb.gateway.age")

router = Router(name="age")

# Miscellaneous.py:191 — v1's own `requests.get(..., timeout=10)` budget, kept:
# there is no join-path urgency here (contrast doomlist.py's 2s), just one
# member waiting on their own command.
_AGIFY_URL = "https://api.agify.io"
_AGIFY_TIMEOUT = httpx.Timeout(10.0)

# Same one-breaker-per-dependency shape as doomlist.py's `_cas_breaker` /
# `_burrbot_breaker` (cb_core/breaker.py's own docstring names this pattern).
_breaker = Breaker()

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient()
    return _client


def set_http_client(client: httpx.AsyncClient | None) -> None:
    """Test seam, same shape as `doomlist.set_http_client`: swap in a client
    backed by `httpx.MockTransport` so tests never touch the real network.
    Pass `None` to go back to a fresh default client.
    """
    global _client
    _client = client


def extract_name(args: str) -> str | None:
    """The first whitespace-separated token of the command's argument text,
    or `None` for "no argument" — v1's `" " not in msg['text']` branch,
    reached here as `not args` (module docstring, deviation 1).
    """
    if not args:
        return None
    return args.split()[0]


@dataclass(frozen=True, slots=True)
class AgeGuess:
    age: int
    registered_times: int


async def _lookup(name: str) -> AgeGuess | None:
    """`GET https://api.agify.io?name=<name>` -> the reported age and its
    sample size, or `None` on any failure (timeout, non-2xx, malformed JSON,
    a missing/non-numeric `count`, or an open breaker) — so the caller falls
    back to the same `not_know` text `count == 0` uses (module docstring,
    deviation 3).

    Returns `None` for a `count == 0` hit too, folding both "no data" shapes
    into one caller-side branch.
    """
    now = time.monotonic()
    if not _breaker.allow(now):
        metrics.external_dep_up.labels(dep="agify").set(0)
        return None

    start = time.perf_counter()
    outcome = "ok"
    try:
        response = await _get_client().get(
            _AGIFY_URL, params={"name": name}, timeout=_AGIFY_TIMEOUT
        )
        response.raise_for_status()
        data: dict[str, Any] = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        outcome = "error"
        log.warning("age.lookup_failed", error=str(exc))
        _breaker.record(False, now)
        metrics.external_dep_up.labels(dep="agify").set(0)
        metrics.external_dep_duration.labels(dep="agify", outcome=outcome).observe(
            time.perf_counter() - start
        )
        return None

    _breaker.record(True, now)
    metrics.external_dep_up.labels(dep="agify").set(1)
    metrics.external_dep_duration.labels(dep="agify", outcome=outcome).observe(
        time.perf_counter() - start
    )
    count = data.get("count")
    age = data.get("age")
    if not isinstance(count, int) or count == 0 or age is None:
        return None
    return AgeGuess(age=age, registered_times=count)


@router.message(CommandName("age"))
async def age_guess(message: Message, parsed: ParsedCommand | None = None) -> None:
    if parsed is None:  # pragma: no cover - CommandName always injects a match
        return

    bot = cast(Bot, message.bot)
    ctx = await context_for(bot, message)
    if await deny_if_disabled(message, ctx, "fun"):
        return

    name = extract_name(parsed.args)
    if name is None:
        await message.reply(t(ctx, "age"))
        return

    guess = await _lookup(name)
    if guess is None:
        await message.reply(t(ctx, "not_know"))
        return

    await message.reply(t(ctx, "age_yes", age=guess.age, registered_times=guess.registered_times))


__all__ = ["AgeGuess", "age_guess", "extract_name", "router", "set_http_client"]
