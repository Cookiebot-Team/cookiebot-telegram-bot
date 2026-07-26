Feature: anti-sticker spam that prevents users from sending excessive stickers in the group

    Background:
        Given that the bot is in the group and properly set up

    Scenario: User sends excessive stickers in the group
        Given that a user sends more than the set amount of stickers within a period of time
        When the bot detects the sticker spam
        Then the bot should issue a warning to the user about excessive sticker usage

    Scenario: The feature is set up to allow sticker spam
        Given that the bot is configured to allow sticker spam
        When a user sends more than the set amount of stickers within a period of time
        Then the bot should not issue any warnings

    # --- Added: real v1 behaviour (Bot/Cooldowns.py:12-22) the spec above never
    # covers. See docs/contracts/core_stickerspam.md Phase 3 for why each was added.

    Scenario: The bot keeps deleting stickers sent after the warning
        Given that a user sends more than the set amount of stickers within a period of time
        When the user sends yet another sticker
        Then the bot should delete the sticker message

    Scenario: Sticker spam is counted per group, not per user
        Given that a user sends stickers just under the set amount within a period of time
        When a different user in the same group sends a sticker that pushes the group past the set amount
        Then the bot should issue a warning to the user about excessive sticker usage

    Scenario: An admin is not exempt from the sticker spam limit
        Given that an admin sends more than the set amount of stickers within a period of time
        Then the bot should issue a warning to the user about excessive sticker usage

    # Boundary table for the warn-at-`==`-limit, delete-at-`>`-limit rule
    # (Bot/Cooldowns.py:12-22, "Counting logic" in docs/contracts/core_stickerspam.md).
    # One row under the limit was never asserted as its own case before (only
    # used as setup for the per-group scenario above) — this makes "nothing
    # happens below the limit" an explicit, checked case alongside the other
    # two boundaries.
    Scenario Outline: The sticker count relative to the limit determines the bot's action
        Given a user sends stickers so that the total is <offset> relative to the limit
        When the bot detects the sticker spam
        Then the bot's resulting action is "<action>"

        Examples:
            | offset | action   |
            | -1     | nothing  |
            | 0      | warning  |
            | 1      | deletion |
