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
    analysis,
    battle,
    birthday,
    calladms,
    chat_ai,
    complaint,
    config_menu,
    deletereposts,
    destroy,
    dice,
    doomlist,
    embedder,
    everyone,
    firecracker,
    fun_random,
    giveaway,
    groupguardian,
    isalive,
    listcommand,
    mediarestrict,
    members,
    meme,
    musicdetection,
    nextbirthday,
    owner,
    postgetter,
    privacy,
    publisher,
    reverse_search,
    rules,
    setlang,
    ship,
    stickerspam,
    transcribe,
    welcome,
    youtube,
)


def build_router() -> Router:
    """Root router. M1 core moderation is live; M2 fun/util, M3 publisher+AI."""
    root = Router(name="root")

    # ---- registry bookkeeping: must see every message, answers none ----
    # v1 registered the sender before dispatch (COOKIEBOT.py:118), so a command
    # in the same message could already read its own author out of the register.
    # Always raises SkipHandler; registering it anywhere but first would mean a
    # command handler consumed the update before the sender was recorded.
    root.include_router(members.router)

    # ---- commands: disjoint triggers, order irrelevant ----
    root.include_router(isalive.router)
    root.include_router(privacy.router)
    # x_analysis sits in v1's ungated stretch of the chain, next to /privacy.
    root.include_router(analysis.router)
    root.include_router(listcommand.router)
    root.include_router(config_menu.router)
    root.include_router(rules.router)
    root.include_router(calladms.router)
    root.include_router(complaint.router)
    root.include_router(dice.router)
    root.include_router(ship.router)
    root.include_router(firecracker.router)
    root.include_router(everyone.router)
    root.include_router(battle.router)
    root.include_router(meme.router)
    root.include_router(youtube.router)
    root.include_router(birthday.router)
    root.include_router(nextbirthday.router)
    # /divulgar, /repost and the three publisher callbacks. Disjoint triggers
    # like the rest of this block; the *reply relay* half of the same feature is
    # order-dependent and is registered further down, on its own router.
    root.include_router(publisher.router)
    root.include_router(deletereposts.router)
    root.include_router(reverse_search.router)
    # x_distortion. Disjoint trigger; the media it acts on is whatever the
    # command replies to, so it never competes with the passive handlers below.
    root.include_router(destroy.router)
    # x_giveaways. `/giveaway` is a disjoint trigger like the rest of this
    # block; its four callbacks are filtered on the `GIVEAWAY ` prefix, which
    # no other callback handler claims (`CONFIG`, `RULES`, the captcha and the
    # publisher's own presses all carry different prefixes).
    root.include_router(giveaway.router)

    # x_owner_commands. Private-chat only and owner-gated, so it shares no
    # trigger with anything above; registered here rather than in the command
    # block because a DM never reaches the join chain or the content rules at
    # all (`.specs/features/private_dispatch/`).
    root.include_router(owner.router)

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
    # util_postforwarder's reply relay. v1 runs `check_notify_post_reply` from
    # an `elif` that sits after the captcha-reply and complaint-reply checks and
    # *before* the conversational-AI branch (COOKIEBOT.py:302-303) — both of
    # those are registered above, and `chat_ai` is registered below. Move this
    # after `chat_ai` and a reply to a published post gets answered by the AI
    # instead of reaching its author. It yields when the reply is to some other
    # bot message with buttons.
    root.include_router(publisher.relay_router)
    # x_conversational_ai: registered immediately before embedder, on purpose
    # (design.md R5.2). v1 only runs `check_reply_embed` in the `else` reached
    # when the AI branch did *not* match (COOKIEBOT.py:309-316), so the embed
    # rewrite must sit downstream of the AI trigger. Every branch that
    # intercepts ahead of the AI in v1 (welcome/rules reply prompts, `who`,
    # the captcha-caption reply, the complaint reply, a reply_markup reply) is
    # already registered earlier above — do not reorder any of it.
    root.include_router(chat_ai.router)
    # core_musicdetection. Ahead of `transcribe` because v1 runs the music
    # check first and unconditionally, while the transcribe->AI sub-step in
    # the same v1 branch has an extra precondition. It always raises
    # SkipHandler, so being first costs the handlers below nothing.
    root.include_router(musicdetection.router)
    # x_speech_to_text: F.voice is disjoint from F.text (chat_ai) and from the
    # command triggers above (F.voice never carries a leading "/"), so its
    # relative order against either is irrelevant (design.md R1.8) -- it only
    # has to stay ahead of embedder/fun_random below, and it does.
    root.include_router(transcribe.router)
    # Link rewriting reacts to ordinary text, which no other handler claims, but
    # it yields like the rest so a message is never consumed on its behalf.
    root.include_router(embedder.router)
    # util_postgetter, and it must stay *ahead* of fun_random. v1's branch for a
    # Telegram-auto-forwarded channel ad is an `elif` that precedes the
    # photo/video branches which pool media into the random library
    # (COOKIEBOT.py:165-172), so an ad is never also collected. This handler
    # replies, so it completes without SkipHandler and propagation stops —
    # which is what reproduces the `elif`. Registered after it, every ad
    # silently joins the pool and nothing errors.
    root.include_router(postgetter.router)
    # Pools every photo/video into the per-group random library, then yields — it
    # is ingestion, not a reply, so it must never consume the update.
    root.include_router(fun_random.router)
    return root


__all__ = ["build_router"]
