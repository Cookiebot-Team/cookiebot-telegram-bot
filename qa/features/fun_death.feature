# Synced from Cookiebot-QA/features/fun_death.feature — the two scenarios
# above the marker are verbatim, wording unchanged. This feature was
# Status.BLOCKED until cb_worker.bucket_export copied v1's `Death/` prefix
# into cb_core.storage and `legacy-catalog` turned the export manifest into
# the per-prefix catalog cb_core.legacy_assets reads (spec.md's "The
# blocker"). This checkout has not run `legacy-catalog` — every scenario
# below therefore seeds a fake pool through legacy_assets.choose rather than
# depending on a real generated catalog.
#
# Scenarios below the marker are additions covering v1 behaviour
# (Miscellaneous.py:335-357, dispatched COOKIEBOT.py:216,218-219,238-239) the
# upstream spec's two scenarios do not exercise: the fun-off gate, the
# reply-based target (branch (2) of spec.md's target-resolution row, which
# neither upstream scenario needs since both use branches (1) and (3)), the
# still-image (non-gif) dispatch, and the empty-pool degrade (D-DE-3) — the
# actual state of this checkout's catalog, not a hypothetical.
Feature: makes a funny post with the user name and tells them how they will die

    Background:
        Given that the bot is in the group and properly set up

    Scenario: user uses the command /death
        Given that the user is in the group
        When the user sends the command /death
        Then the bot should reply with a meme and a random skull gif
        And random cause of death for the user

    Scenario: user uses the command /death with another user tagged
        Given that the user is in the group
        When the user sends the command /death and tags another user
        Then the bot should reply with a meme and a random skull gif
        And random cause of death for the tagged user

    # --- Scenarios below this line were not in the original Cookiebot-QA spec.
    # Added while porting to v2 to cover v1 behaviour this spec never exercises.

    Scenario: Fun functions are switched off for the group
        Given that fun functions are disabled for the group
        When the user sends the command /death
        Then the bot replies that fun functions are off

    Scenario Outline: Every v1 trigger spelling works
        Given that the user is in the group
        When the user sends the command "<command>"
        Then the bot should reply with a meme and a random skull gif

        Examples:
            | command |
            | /morte  |
            | /muerte |

    Scenario: Replying to someone names them as the cause of death
        Given that the user is in the group
        When the user sends the command /death as a reply to another user's message
        Then random cause of death for the replied-to user

    Scenario: A still image from the pool is sent as a photo, not an animation
        Given that the death pool's chosen entry is a still image
        When the user sends the command /death
        Then the bot sends a photo, not an animation

    Scenario: The catalog has not been generated in this deployment yet
        Given that the death asset pool is empty
        When the user sends the command /death
        Then the user receives no response
