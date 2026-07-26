"""Router registration — and, for join events, the order *is* the behaviour.

v1 handled a join with one `if/elif` chain (`COOKIEBOT.py:136-149`), so exactly
one branch ever ran: a doomlist hit meant no captcha and no welcome, and a
captcha meant no welcome until it was solved. aiogram stops at the first router
that handles an update, which reproduces that faithfully — but only if the
routers are registered in the same order, and only if every handler that decides
"not mine" raises `SkipHandler` instead of returning quietly.

Get this wrong and nothing errors: one feature silently swallows every join and
the rest never run. Hence explicit, commented order rather than alphabetical.
"""

from aiogram import Router

from cb_gateway.handlers import (
    calladms,
    config_menu,
    dice,
    doomlist,
    embedder,
    fun_random,
    groupguardian,
    isalive,
    listcommand,
    mediarestrict,
    privacy,
    rules,
    setlang,
    stickerspam,
    welcome,
)


def build_router() -> Router:
    """Root router. M1 core moderation is live; M2 fun/util, M3 publisher+AI."""
    root = Router(name="root")

    # ---- commands: disjoint triggers, order irrelevant ----
    root.include_router(isalive.router)
    root.include_router(privacy.router)
    root.include_router(listcommand.router)
    root.include_router(config_menu.router)
    root.include_router(rules.router)
    root.include_router(calladms.router)
    root.include_router(dice.router)

    # ---- join chain: order matters, see the module docstring ----
    # 1. Bookkeeping first. `group_members.joined_at` is recorded even for a
    #    member the doomlist is about to ban, so media restriction can answer
    #    "how long has this member been here?" for everyone who stays. Yields.
    root.include_router(mediarestrict.router)
    # 2. The bot's own join — derives the group's language from whoever added it.
    root.include_router(setlang.router)
    # 3. Listed users are removed before anything greets them (v1's doomlist
    #    `elif` precedes both the captcha and the welcome).
    root.include_router(doomlist.router)
    # 4. Captcha, when enabled and the bot can enforce it. v1 sends the welcome
    #    only once the captcha is solved, never alongside it.
    root.include_router(groupguardian.router)
    # 5. Welcome is the fallthrough, exactly as v1's trailing `else`.
    root.include_router(welcome.router)

    # ---- content rules ----
    # mediarestrict is registered above and yields when it does not act, so a new
    # member's sticker is judged as restricted media first and only then as one
    # more sticker in the flood counter. v1 never faced the overlap: Telegram
    # blocked a restricted member's sticker client-side.
    root.include_router(stickerspam.router)
    # Link rewriting reacts to ordinary text, which no other handler claims, but
    # it yields like the rest so a message is never consumed on its behalf.
    root.include_router(embedder.router)
    # Pools every photo/video into the per-group random library, then yields — it
    # is ingestion, not a reply, so it must never consume the update.
    root.include_router(fun_random.router)
    return root


__all__ = ["build_router"]
