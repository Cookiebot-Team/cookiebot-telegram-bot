"""x_gender_guess — `/genero`, `/gênero`, `/gender`: guess a name's gender via
genderize.io.

v1: `gender`, `../COOKIEBOT-Telegram-Group-Bot/Bot/Miscellaneous.py:204-224`::

    def gender(cookiebot, msg, chat_id, language):
        send_chat_action(cookiebot, chat_id, 'typing')
        if not " " in msg['text']:
            text = i18n.get("gender_exemple", lang=language)
            send_message(cookiebot, chat_id, text, msg)
        else:
            nome = msg['text'].replace("/genero ", '').replace("/gênero ", '') \\
                .replace("/gender ", '').replace("/genero@CookieMWbot", '') \\
                .replace("/gênero@CookieMWbot", '').replace("/gender@CookieMWbot", '').split()[0]
            response = json.loads(requests.get(f"https://api.genderize.io?name={nome}", timeout=10).text)
            genero = response['gender']
            probability = response['probability']
            registered_times = response['count']
            if registered_times == 0:
                text = i18n.get("not_know", lang=language)
                send_message(cookiebot, chat_id, text, msg)
            else:
                ctx = {"probability": probability*100, "registered_times": registered_times}
                text = i18n.get(f"gender.{genero}", lang=language, **ctx)
                send_message(cookiebot, chat_id, text, msg)

Dispatched at `COOKIEBOT.py:228-229`, the same `funfunctions`-gated block as
`age.py` (see that module's docstring for the shared `fun_off` shape — this
handler checks the gate itself and replies, rather than the silent
`FeatureGate` filter).

## Deviations from v1, and why

1-3. **Argument parsing, URL encoding, external-failure fallback** — identical
   reasoning to `age.py`'s deviations 1-3; not repeated here. `not_know` is
   this handler's fallback for a genderize timeout/error too.
4. **`gender: null` with a non-zero `count`.** genderize.io's documented
   contract only returns a null gender when `count == 0` — the branch already
   intercepted above — so this should be unreachable in production. v1 does
   not special-case it: `f"gender.{genero}"` with `genero = None` builds the
   literal key `"gender.None"`. v1's own `Localizer.get(key, lang,
   default=None, **fmt)` (`loc.py:83-99`) returns its `default` — `None` — for
   any key it cannot find, so `text` becomes `None` and the ensuing
   `send_message` call fails Telegram's API validation; uncaught, the same
   "answers nothing" outcome as every other unhandled exception in v1's
   dispatcher. There is no v1 *reply* to preserve here, only v1 *data* worth
   noticing: `Bot/Static/locales/eng/lib.json`'s own `gender` object already
   ships a third entry, `"unknown"`, byte-for-byte copied into
   `cb_core/locale_data/en/lib.json` (`locales.py`'s `_load_catalog` docstring)
   — dead in v1 because no code path ever asks for it, present in `en` only
   (not `pt`/`es`), and shaped with a *different* placeholder,
   `%(probability_str)s` rather than `%(probability)s`. This port is the first
   code to reach for it: any gender value other than `"male"`/`"female"`
   (null included) renders `gender.unknown` instead of crashing or going
   silent. `probability_str` is built as a string rather than reusing the
   numeric `probability` substitution because the entry's own placeholder
   name says a null gender's probability is not the same kind of fact as
   male/female's — `"?"` when genderize did not report a number, otherwise
   the same `round(probability * 100)` shape the other two branches use. A
   `pt`/`es` group hitting this (should-be-unreachable) branch is answered in
   English, via `get_nested`'s existing per-entry fallback — the same D-PG-3
   precedent `locales.py` documents for a `cb.json`-only key.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, cast

import httpx
from aiogram import Bot, Router
from aiogram.types import Message

from cb_core import locales, metrics
from cb_core.breaker import Breaker
from cb_core.logging import get_logger
from cb_core.textmatch import ParsedCommand
from cb_gateway.context import ChatContext, context_for, deny_if_disabled, t
from cb_gateway.filters import CommandName

log = get_logger("cb.gateway.gender")

router = Router(name="gender")

# Miscellaneous.py:210 — v1's own `requests.get(..., timeout=10)` budget, kept
# (same reasoning as age.py: no join-path urgency here).
_GENDERIZE_URL = "https://api.genderize.io"
_GENDERIZE_TIMEOUT = httpx.Timeout(10.0)

_breaker = Breaker()

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient()
    return _client


def set_http_client(client: httpx.AsyncClient | None) -> None:
    """Test seam, same shape as `doomlist.set_http_client` / `age.set_http_client`."""
    global _client
    _client = client


def extract_name(args: str) -> str | None:
    """Identical contract to `age.extract_name` — see that function's docstring."""
    if not args:
        return None
    return args.split()[0]


@dataclass(frozen=True, slots=True)
class GenderGuess:
    gender: str | None
    probability: float | None
    registered_times: int


async def _lookup(name: str) -> GenderGuess | None:
    """`GET https://api.genderize.io?name=<name>`. `None` on any failure
    (timeout, non-2xx, malformed JSON, a missing/non-numeric `count`, or an
    open breaker) — the caller falls back to the same `not_know` text
    `count == 0` uses (module docstring, deviations 1-3).
    """
    now = time.monotonic()
    if not _breaker.allow(now):
        metrics.external_dep_up.labels(dep="genderize").set(0)
        return None

    start = time.perf_counter()
    outcome = "ok"
    try:
        response = await _get_client().get(
            _GENDERIZE_URL, params={"name": name}, timeout=_GENDERIZE_TIMEOUT
        )
        response.raise_for_status()
        data: dict[str, Any] = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        outcome = "error"
        log.warning("gender.lookup_failed", error=str(exc))
        _breaker.record(False, now)
        metrics.external_dep_up.labels(dep="genderize").set(0)
        metrics.external_dep_duration.labels(dep="genderize", outcome=outcome).observe(
            time.perf_counter() - start
        )
        return None

    _breaker.record(True, now)
    metrics.external_dep_up.labels(dep="genderize").set(1)
    metrics.external_dep_duration.labels(dep="genderize", outcome=outcome).observe(
        time.perf_counter() - start
    )
    count = data.get("count")
    if not isinstance(count, int) or count == 0:
        return None
    probability = data.get("probability")
    return GenderGuess(
        gender=data.get("gender"),
        probability=probability if isinstance(probability, int | float) else None,
        registered_times=count,
    )


def render_reply(ctx: ChatContext, guess: GenderGuess) -> str:
    """`gender.<male|female>` formatted with `probability`/`registered_times`,
    or the `gender.unknown` fallback for anything else (module docstring,
    deviation 4). A pure function of the parsed API result so the fallback
    shape is unit-testable without a network or a Bot.

    `gender` is a *nested* catalog object (`{"male": ..., "female": ...,
    "unknown": ...}`), not a flat key — `locales.get_nested`, not `t()`
    (`t()` wraps the flat `locales.get`; see `context.py`).
    """
    if guess.gender in ("male", "female"):
        probability_pct = (guess.probability or 0.0) * 100
        return locales.get_nested(
            "gender",
            guess.gender,
            ctx.lang,
            probability=round(probability_pct),
            registered_times=guess.registered_times,
        )
    probability_str = "?" if guess.probability is None else str(round(guess.probability * 100))
    return locales.get_nested(
        "gender",
        "unknown",
        ctx.lang,
        probability_str=probability_str,
        registered_times=guess.registered_times,
    )


@router.message(CommandName("gender"))
async def gender_guess(message: Message, parsed: ParsedCommand | None = None) -> None:
    if parsed is None:  # pragma: no cover - CommandName always injects a match
        return

    bot = cast(Bot, message.bot)
    ctx = await context_for(bot, message)
    if await deny_if_disabled(message, ctx, "fun"):
        return

    name = extract_name(parsed.args)
    if name is None:
        await message.reply(t(ctx, "gender_exemple"))
        return

    guess = await _lookup(name)
    if guess is None:
        await message.reply(t(ctx, "not_know"))
        return

    await message.reply(render_reply(ctx, guess))


__all__ = [
    "GenderGuess",
    "extract_name",
    "gender_guess",
    "render_reply",
    "router",
    "set_http_client",
]
