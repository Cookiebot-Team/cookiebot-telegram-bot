# Synced from Cookiebot-QA/features/fun_battle.feature.
#
# QA's "tags another user" (singular) is v1's one-tag path
# (SocialContent.py:345-357): that person's photo vs. a randomly-picked
# "fighter" character image. The fighter pool is a private GCS bucket
# (Fight/English, Fight/Portuguese) never checked into the v1 repo -- the
# exact same blocker fun_death's Death/ prefix hit, recorded in
# .specs/features/fun_death/spec.md and .specs/features/fun_battle/spec.md.
# This slice ships the two-person shape only (explicit tags, or "random"),
# which needs no bucket at all; the one-tag and no-tag shapes reply v1's own
# battle_no_picture -- an already-ported, literally true string -- until the
# bucket is exported. The scenario below is therefore not runnable yet;
# qa/test_fun_battle.py's step for it calls pytest.skip() with this same
# reason rather than silently passing or asserting something untrue. The
# scenarios after it are net-new, covering what this slice actually ships.
Feature: bot makes a poll in the group, and users can vote on it on who would win in a battle

    Background:
        Given that the bot is in the group and properly set up

    Scenario: User creates a poll and users vote on it
        Given that the user is a member of the group
        When the user types the command /battle
        And tags another user in the group
        Then the bot should display a message "Who would win in a battle?" with options "Option A" and "Option B"
        And makes a poll in which the users can vote on who would win in a battle

    # --- Scenarios below this line were not in the original Cookiebot-QA
    # spec. Added while porting to v2 to cover the shape that actually ships
    # in this slice (two people, explicit tags or "random") plus its failure
    # paths and the fun-off gate.

    Scenario: Two tagged users battle each other
        Given that the user is a member of the group
        And that two other members are registered in the group
        When the user tags both other members in a /battle command
        Then the bot should post a two-photo battle and a poll naming both tagged members

    Scenario: "random" picks two registered members
        Given that the user is a member of the group
        And that two other members are registered in the group
        When the user sends the command /battle random
        Then the bot should post a two-photo battle and a poll naming two registered members

    Scenario: "random" with too few registered members
        Given that the user is a member of the group
        And that no other members are registered in the group
        When the user sends the command /battle random
        Then the bot should reply that not enough members are known to battle

    Scenario: A tagged user who has never spoken in the group cannot be resolved
        Given that the user is a member of the group
        And that two other members are registered in the group
        When the user tags a stranger and a registered member in a /battle command
        Then the bot should reply that it could not extract the stranger's photo

    Scenario: A bare /battle with no tag and no "random" is not implemented yet
        Given that the user is a member of the group
        When the user sends the command /battle
        Then the bot should reply that a profile picture is needed

    Scenario: The fun feature is turned off
        Given that the user is a member of the group
        And the fun feature is turned off
        When the user tags both other members in a /battle command
        Then the bot should reply with a message saying that the fun feature is turned off
