"""util_doomlist — block listed users from joining.

v1: `check_cas`/`check_banlist`/`check_banlist_public`
(`../COOKIEBOT-Telegram-Group-Bot/Bot/GroupShield.py:193-229`), wired at
`../COOKIEBOT-Telegram-Group-Bot/Bot/COOKIEBOT.py:142` as part of the join
dispatch chain: `check_human(...) or check_cas(...) or check_banlist(...) or
check_banlist_public(...)`.

QA: `Cookiebot-QA/features/util_doomlist.feature` -> `qa/features/util_doomlist.feature`.
Contract: `docs/contracts/util_doomlist.md` — read that first for the full v1/v2
table, the exact endpoint shapes quoted from v1, and the fail-open reasoning for
both external calls.

Scope decisions, out of this port (full reasoning in the contract doc):

- `check_human` (`GroupShield.py:172-191`, "no username + no profile photo ->
  kick") is a *different* heuristic — bot suspicion, not "listed user" — and
  `docs/site/content/docs/feature-map.mdx`'s own `util_doomlist` row names only the three
  functions above. It belongs to whichever feature owns
  `core_groupguardian`'s bot-suspicion heuristics.
- The `funfunctions`-gated 1-in-10 "silence_scammer.jpg" photo
  (`COOKIEBOT.py:143-145`) was deferred by this port for want of a static
  asset it did not own. **`core_botskins` now owns it** — the asset is
  `cb_core/asset_data/doomlist/silence_scammer.jpg` and the gate is
  `cb_core.skins.scammer_photo_allowed`, because v1's condition is
  `funfunctions or is_alternate_bot`: an event skin posts it even in a group
  that has switched fun features off. Added below, in `on_join`.

Wiring note for whoever owns `handlers/__init__.py` (not this task): `router`
must be registered **before** `welcome.router` (and before whatever ends up
registering `core_groupguardian`'s captcha), because v1 only reaches
welcome/captcha when none of the ban checks fired
(`COOKIEBOT.py:142`'s `elif` chain). This handler raises `SkipHandler` on every
non-hit path for exactly that reason — see `docs/contracts/core_welcome.md`'s
own note about this same ordering problem, from the other side.
"""

from __future__ import annotations

import json
import random
import time
from typing import cast

import httpx
from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.types import FSInputFile, Message, User

from cb_core import db, metrics, skins
from cb_core.breaker import Breaker
from cb_core.logging import get_logger
from cb_gateway.context import ChatContext, context_for, t

log = get_logger("cb.doomlist")

router = Router(name="doomlist")

# GroupShield.py:195 — cas.chat's own budget in v1; kept identical.
_CAS_URL = "https://api.cas.chat/check"
_CAS_TIMEOUT = httpx.Timeout(2.0)

# GroupShield.py:218 had *no* timeout at all (`requests.post` with no
# `timeout=` blocks forever on a stalled connection) — one of the two named
# defects this port's task brief calls out. Given the same budget as cas.chat:
# both are best-effort join gates of equal importance, and a slower value on
# either would let one flaky vendor delay every join in every group.
_BURRBOT_URL = "https://burrbot.xyz/noraid.php"
_BURRBOT_TIMEOUT = httpx.Timeout(2.0)

# GroupShield.py:210 — swastika + two look-alike glyphs used by raid accounts
# to dodge text filters. Copied verbatim, not re-derived.
_FORBIDDEN_NAME_CHARS = ("卐", "ζ", "𝛇")

# One breaker per dependency, same pattern as cb_core.llm.router's
# per-provider breakers (cb_core/breaker.py's own docstring names this port as
# the reason the class lives in cb_core rather than inside the LLM router).
_cas_breaker = Breaker()
_burrbot_breaker = Breaker()

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient()
    return _client


def set_http_client(client: httpx.AsyncClient | None) -> None:
    """Test seam: swap in a client backed by `httpx.MockTransport` so unit and
    acceptance tests can simulate cas.chat / burrbot (including "the third
    party is down") without any real network access. Pass `None` to go back to
    a fresh default client.

    TLS verification is never disabled here, in either the default client or
    what a test injects for a real deployment path: a host that genuinely
    needed a `verify=False` exception would be a finding for
    `docs/contracts/util_doomlist.md`, not a silent flag flip (v1's own D2,
    `universal_funcs.py:100`, only afflicts a call this port does not make —
    see the contract's "v2 architecture" section).
    """
    global _client
    _client = client


# --------------------------------------------------------------- external checks


async def check_cas(user_id: int) -> bool:
    """GroupShield.py:193-204. `GET {_CAS_URL}?user_id=<id>`; hit ==
    `bool(response.json()["ok"])`. Fails open on any error, timeout, breaker-open
    or malformed body — never "not sure, block anyway". See the contract's
    fail-open section for why: a join must never hang or fail because cas.chat
    is unavailable.
    """
    now = time.monotonic()
    if not _cas_breaker.allow(now):
        metrics.external_dep_up.labels(dep="cas").set(0)
        return False

    start = time.perf_counter()
    outcome = "ok"
    try:
        response = await _get_client().get(
            _CAS_URL, params={"user_id": user_id}, timeout=_CAS_TIMEOUT
        )
        hit = bool(response.json().get("ok"))
    except (httpx.HTTPError, ValueError) as exc:
        outcome = "error"
        log.warning("doomlist.cas_failed", error=str(exc))
        _cas_breaker.record(False, now)
        metrics.external_dep_up.labels(dep="cas").set(0)
        metrics.external_dep_duration.labels(dep="cas", outcome=outcome).observe(
            time.perf_counter() - start
        )
        return False

    _cas_breaker.record(True, now)
    metrics.external_dep_up.labels(dep="cas").set(1)
    metrics.external_dep_duration.labels(dep="cas", outcome=outcome).observe(
        time.perf_counter() - start
    )
    return hit


async def check_burrbot(user_id: int) -> bool:
    """GroupShield.py:217-229. `POST {_BURRBOT_URL}` with `data={"id": str(id)}`;
    hit == `bool(parsed["raider"])`. The response body doubles its own quotes
    (`{"raider":"false""}`-shaped, observed in production); v1 works around this
    rather than treating it as an error, and so does this port. Fails open on
    any error, timeout, breaker-open or malformed body, same reasoning as
    `check_cas`.
    """
    now = time.monotonic()
    if not _burrbot_breaker.allow(now):
        metrics.external_dep_up.labels(dep="burrbot").set(0)
        return False

    start = time.perf_counter()
    outcome = "ok"
    try:
        response = await _get_client().post(
            _BURRBOT_URL, data={"id": str(user_id)}, timeout=_BURRBOT_TIMEOUT
        )
        cleaned = response.text.replace('""', '"')
        hit = bool(json.loads(cleaned).get("raider"))
    except (httpx.HTTPError, ValueError) as exc:
        outcome = "error"
        log.warning("doomlist.burrbot_failed", error=str(exc))
        _burrbot_breaker.record(False, now)
        metrics.external_dep_up.labels(dep="burrbot").set(0)
        metrics.external_dep_duration.labels(dep="burrbot", outcome=outcome).observe(
            time.perf_counter() - start
        )
        return False

    _burrbot_breaker.record(True, now)
    metrics.external_dep_up.labels(dep="burrbot").set(1)
    metrics.external_dep_duration.labels(dep="burrbot", outcome=outcome).observe(
        time.perf_counter() - start
    )
    return hit


async def _persist_cas_hit(user_id: int) -> None:
    """GroupShield.py:200, `ban_and_blacklist` (`universal_funcs.py:315-318`): a
    CAS hit is also written to the (global) blacklist, so a later join anywhere
    skips straight to the free local check instead of paying for cas.chat again.

    Best-effort: losing this write must not turn an otherwise-successful ban
    into a failed reply (AGENTS.md §2.6 — same posture as `llm.router`'s
    `_persist` for `llm_usage`).
    """
    try:
        await db.execute(
            """
            INSERT INTO blacklist (subject_id, kind, reason, source)
            VALUES ($1, 'user', 'flagged by cas.chat on join', 'cas')
            ON CONFLICT (subject_id) DO NOTHING
            """,
            user_id,
            name="doomlist_cas_persist",
        )
    except Exception as exc:  # noqa: BLE001 - a lost persist must not block the ban
        log.warning("doomlist.blacklist_persist_failed", error=str(exc), user_id=user_id)


# ------------------------------------------------------------------ local check


def _has_forbidden_chars(full_name: str) -> bool:
    """GroupShield.py:210's `any(forbidden_char in fullname for forbidden_char in
    [...])`. `User.full_name` (aiogram) already computes v1's own
    `f"{first_name} {last_name}"` / `first_name` fallback, so no re-derivation
    is needed here.
    """
    return any(ch in full_name for ch in _FORBIDDEN_NAME_CHARS)


async def check_local_blacklist(newcomer: User) -> bool:
    """GroupShield.py:206-215: blacklist by id, blacklist by username, and a
    forbidden-character check on the full name — any one hit blocks. v1's two
    HTTP reads against the Java backend (`blacklist/{id}`,
    `blacklist/username/{username}`, both through `get_request_backend`'s
    `verify=False, timeout=60` — FEATURE-MAP D2) become one query against the
    reference `blacklist` table (migration 0001), replicated to every node — no
    network round trip, no breaker needed (AGENTS.md §4: filtered reference-
    table reads are node-local).

    The username branch joins through `users.username` rather than a
    `blacklist.username` column, because `blacklist.subject_id` is `bigint`-only
    in the owned migration — exact whenever the blacklisted account's username
    has ever been recorded in `users`; see the contract's "v2 architecture"
    section for why this is the closest available equivalent, not a schema
    change this task is scoped to make.
    """
    if _has_forbidden_chars(newcomer.full_name):
        return True

    row = await db.fetchrow(
        """
        SELECT EXISTS (
            SELECT 1 FROM blacklist WHERE kind = 'user' AND subject_id = $1
        ) OR EXISTS (
            SELECT 1 FROM blacklist b
            JOIN users u ON u.user_id = b.subject_id
            WHERE b.kind = 'user' AND $2::text IS NOT NULL AND lower(u.username) = lower($2)
        ) AS hit
        """,
        newcomer.id,
        newcomer.username,
        name="doomlist_blacklist_lookup",
    )
    return bool(row["hit"]) if row is not None else False


# --------------------------------------------------------------------- dispatch


async def _evaluate(newcomer: User) -> str | None:
    """v1's dispatch order, preserved exactly (`COOKIEBOT.py:142`, minus
    `check_human` — see the module docstring's Scope section): CAS first, then
    the local/backend blacklist, then burrbot. Returns the locale key for the
    hit's user-facing text, or `None` if nothing matched.

    Reordering for "cheapest check first" would change *which* text a
    doubly-listed user sees, which is an observable regression, not just an
    optimisation — see `docs/contracts/util_doomlist.md`.
    """
    if await check_cas(newcomer.id):
        await _persist_cas_hit(newcomer.id)
        return "ban_cas"
    if await check_local_blacklist(newcomer):
        return "ban"
    if await check_burrbot(newcomer.id):
        return "ban"
    return None


#: v1's `random.randint(1, 10) == 1` (`COOKIEBOT.py:143`).
FLAIR_ODDS = 10

#: Test seam, same shape as `set_http_client` above. The flair is the only
#: non-deterministic thing this handler does, and a 1-in-10 extra photo turns
#: every ban scenario into an occasional surprise — the kind of flapping this
#: project already refused once (HANDOFF §6.2). Production leaves it `None`
#: and uses the shared `random` module state.
_flair_rng: random.Random | None = None


def set_flair_rng(rng: random.Random | None) -> None:
    global _flair_rng
    _flair_rng = rng


async def _maybe_post_flair(
    message: Message, ctx: ChatContext, skin: str, rng: random.Random | None = None
) -> None:
    """v1's one-in-ten "silence scammer" photo (`COOKIEBOT.py:143-145`).

    Gated on `funfunctions or is_alternate_bot` — see
    `cb_core.skins.scammer_photo_allowed` for why an event skin ignores the
    group's fun switch here. Best-effort: the ban and the notice have already
    happened, and a missing asset or a send failure must not turn a successful
    block into a handler error.
    """
    if not skins.scammer_photo_allowed(skin, fun_enabled=ctx.enabled("fun")):
        return
    source = rng or _flair_rng
    roll = source.randint(1, FLAIR_ODDS) if source is not None else random.randint(1, FLAIR_ODDS)
    if roll != 1:
        return
    try:
        path = skins.asset(skin, "doomlist", "silence_scammer.jpg")
        await message.answer_photo(FSInputFile(path))
    except Exception as exc:  # noqa: BLE001 - cosmetic; never worth failing a block over
        log.warning("doomlist.flair_failed", error=str(exc))


@router.message(F.new_chat_members)
async def on_join(message: Message, skin: str = skins.PRIMARY_SKIN) -> None:
    joiners = message.new_chat_members
    if not joiners:
        raise SkipHandler("no joiners in update")

    # Same v1 quirk `core_welcome.py` already documents: only the deprecated
    # singular `new_chat_participant` (== new_chat_members[0]) is ever read.
    newcomer = joiners[0]
    bot = cast(Bot, message.bot)

    if newcomer.id == bot.id:
        raise SkipHandler("bot's own join is not this feature's concern")

    from_user = message.from_user
    if from_user is None or from_user.id != newcomer.id:
        # COOKIEBOT.py:136: the ban-check chain only runs on a self-join
        # (`msg['from']['id'] == new_chat_participant['id']`). An existing
        # member adding someone else skips straight to welcome/captcha in v1 -
        # preserved exactly, see the contract's "Known defects" row.
        raise SkipHandler("not a self-join")

    if newcomer.is_bot:
        # v1's `is_bot` branch only exists inside the *not-self-join* arm
        # (COOKIEBOT.py:137-139); a bot account self-joining is not a shape
        # v1's dispatch models. Defer rather than guess.
        raise SkipHandler("bot account, not modelled by v1's self-join branch")

    ctx = await context_for(bot, message)
    if not ctx.config.doomlist_enabled:
        raise SkipHandler("doomlist disabled for this group")

    hit_key = await _evaluate(newcomer)
    if hit_key is None:
        raise SkipHandler("no list matched")

    # v1 has no try/except around `kickChatMember` in any of the three
    # functions this ports (GroupShield.py:200-203, 211-213, 225-227): if the
    # bot lacks ban rights, the exception propagates and no message is sent
    # either. Preserved exactly - not wrapped in a best-effort guard here.
    await bot.ban_chat_member(message.chat.id, newcomer.id)
    await message.answer(t(ctx, hit_key))
    await _maybe_post_flair(message, ctx, skin)


__all__ = [
    "FLAIR_ODDS",
    "check_burrbot",
    "check_cas",
    "check_local_blacklist",
    "on_join",
    "router",
    "set_flair_rng",
    "set_http_client",
]
