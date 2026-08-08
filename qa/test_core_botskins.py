"""Step definitions for core_botskins.

QA: `qa/features/core_botskins.feature`, synced from
`../Cookiebot-QA/features/core_botskins.feature` — the three scenarios that
had never been ported (`.specs/features/core_botskins/spec.md`: "0 of 3"),
plus two this port adds for the flagship-only behaviour an event-skin scenario
cannot show.

The QA scenarios do not say what "display the skin and provide event-specific
interactions" means, so the Then step binds it to what a skin observably *is*
in v1 and now in v2:

1. **The brand.** `cb_core.skins.display_name` resolves through the tenant
   registry — "Bombot", "Pawsy", "Tarinbot" — rather than every skin
   answering as Cookiebot. Migration 0007 is what made the last three
   resolvable at all.
2. **Event-specific interaction.** v1 has exactly two behaviours keyed on
   `is_alternate_bot` that are still meaningful (`cb_core/skins.py` explains
   why the other two are not): an event skin posts the join-time flair even
   when the group has fun features off, and it does *not* post the flagship's
   introduction animation.

Needs a real database: the tenant registry reads `tenants`, and the flair
scenario reads the group's `functions_fun`.
"""

from __future__ import annotations

from types import ModuleType
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from pytest_bdd import given, parsers, scenarios, then, when

from cb_core import skins
from qa.conftest import (
    USER_ID,
    Context,
    feed,
    make_join_update,
    make_message_update,
    next_update_id,
)
from qa.mock_telegram import MockTelegram

scenarios("core_botskins.feature")

#: QA's skin names, and the tenant id each one is the display name of. The
#: mismatch on "Pawsy" is v1's own — see migration 0007's docstring.
SKIN_IDS = {"Bombot": "bombot", "Pawsy": "pawstralbot", "Tarinbot": "tarinbot"}

BOT_ID = 424242


class SkinCtx:
    def __init__(self) -> None:
        self.skin: str = skins.PRIMARY_SKIN
        self.display: str = "Cookiebot"


@pytest.fixture
def skin_ctx() -> SkinCtx:
    return SkinCtx()


@pytest.fixture(autouse=True)
def _tenants(database: ModuleType, run: Any) -> None:
    """Load every skin's tenant row into the registry's process cache.

    `skins.display_name` is deliberately synchronous — a handler must not open
    a database connection for a label — so the row has to be in the cache
    before it is asked for. In production `cb_gateway.main` warms it at
    startup for each configured skin.
    """
    from cb_core import tenancy

    for tenant_id in (skins.PRIMARY_SKIN, *SKIN_IDS.values()):
        run(tenancy.registry.by_skin(tenant_id))
        run(tenancy.registry.by_id(tenant_id))
        # `by_skin` joins through `bots`, which has no row for an unconfigured
        # persona, so it answers FALLBACK; `by_id` is what actually resolves
        # the tenant. Seed the skin key from it so the synchronous lookup finds
        # the right brand either way.
        tenancy.registry._local[f"skin:{tenant_id}"] = tenancy.registry._local[  # noqa: SLF001
            tenant_id
        ]


# ---------------------------------------------------------------------- given


@given("that the bot is in the group and properly set up")
def bot_set_up(ctx: Context) -> None:
    ctx.bot_running = True


@given(parsers.parse('that the bot skin "{name}" is applied to Cookiebot'))
def skin_applied(skin_ctx: SkinCtx, name: str) -> None:
    skin_ctx.skin = SKIN_IDS[name]
    skin_ctx.display = name


@given(parsers.parse('the bot is on the "{event}" event group'))
def on_event_group(event: str) -> None:
    """The QA spec's framing. There is no per-event group registry in v1 or
    v2 — a skin is invited to a group, and the group is whichever one it was
    invited to. Nothing to set up; the assertion is about the skin."""


# ----------------------------------------------------------------------- when


@when("the user interacts with the bot in the group")
def user_interacts(
    run: Any, dispatcher: Dispatcher, bot: Bot, skin_ctx: SkinCtx, telegram: MockTelegram
) -> None:
    feed(
        run,
        dispatcher,
        bot,
        make_message_update("/isalive", next_update_id(), user_id=USER_ID),
        skin=skin_ctx.skin,
    )


@when("the flagship bot is added to a group")
def flagship_added(run: Any, dispatcher: Dispatcher, bot: Bot) -> None:
    _feed_bot_join(run, dispatcher, bot, skins.PRIMARY_SKIN)


@when("that skin's bot is added to a group")
def skin_added(run: Any, dispatcher: Dispatcher, bot: Bot, skin_ctx: SkinCtx) -> None:
    _feed_bot_join(run, dispatcher, bot, skin_ctx.skin)


def _feed_bot_join(run: Any, dispatcher: Dispatcher, bot: Bot, skin: str) -> None:
    update = make_join_update(next_update_id(), joiners=[(BOT_ID, "Cookiebot")])
    feed(run, dispatcher, bot, update, skin=skin)


# ----------------------------------------------------------------------- then


@then(
    parsers.parse(
        'the bot should display the "{name}" skin and provide event-specific interactions'
    )
)
def displays_the_skin(skin_ctx: SkinCtx, telegram: MockTelegram, name: str) -> None:
    skin = SKIN_IDS[name]
    # 1. The brand resolves to this skin's own name, not the flagship's.
    assert skins.display_name(skin) == name
    assert skins.display_name(skin) != skins.display_name(skins.PRIMARY_SKIN)

    # 2. Event-specific interaction: this skin behaves differently from the
    #    flagship in both places v1 keys on `is_alternate_bot`.
    assert not skins.is_primary(skin)
    assert skins.scammer_photo_allowed(skin, fun_enabled=False)
    assert not skins.scammer_photo_allowed(skins.PRIMARY_SKIN, fun_enabled=False)
    assert not skins.posts_intro_animation(skin)

    # 3. It is really this bot answering — the interaction produced a reply.
    assert telegram.calls_to("sendMessage"), "the skin did not answer at all"


@then("the bot should post its introduction animation")
def posts_animation(telegram: MockTelegram) -> None:
    sent = telegram.calls_to("sendAnimation")
    assert sent, "the flagship did not announce itself"
    assert sent[-1]["animation"] == skins.INTRO_ANIMATION_URL


@then("the bot should not post an introduction animation")
def posts_no_animation(telegram: MockTelegram) -> None:
    assert telegram.calls_to("sendAnimation") == []
