# Synced from Cookiebot-QA/features/fun_battle.feature.
#
# QA's "tags another user" (singular) is v1's one-tag path
# (SocialContent.py:345-357): that person's photo vs. a randomly-picked
# "fighter" character image from the Fight/English and Fight/Portuguese
# prefixes of v1's private GCS bucket. That bucket has since been exported
# and catalogued, so the scenario below runs for real -- it was skipped in
# the slice that shipped only the two-person shape. QA's wording ("Option A",
# "Option B") does not name the real poll options, which are the two display
# names; the step definitions drive v1's actual behaviour underneath, the
# same pattern util_everyone's "/ping everyone" mismatch uses.
#
# The scenarios below the marker are net-new, covering the shapes and
# failure paths QA never wrote one for.
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
    # spec. Added while porting to v2 to cover every shape v1 has (two
    # people by tag or "random", one tag against a fighter, the caller
    # against a fighter) plus each failure path and the fun-off gate.

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

    Scenario: A bare /battle pits the caller against a fighter
        Given that the user is a member of the group
        And the caller has a profile picture
        When the user sends the command /battle
        Then the bot should post a battle and a poll naming the caller and a fighter

    Scenario: The caller has no profile picture
        Given that the user is a member of the group
        And the caller has no profile picture
        When the user sends the command /battle
        Then the bot should reply that a profile picture is needed

    Scenario: A tagged user with no visible profile picture
        Given that the user is a member of the group
        And that two other members are registered in the group
        And the tagged member's profile picture is not visible
        When the user tags a registered member in a /battle command
        Then the bot should reply that the tagged user's picture is private

    Scenario: The fighter pool has never been catalogued
        Given that the user is a member of the group
        And the caller has a profile picture
        And the fighter pool is empty
        When the user sends the command /battle
        Then the bot should send nothing at all

    Scenario: The fun feature is turned off
        Given that the user is a member of the group
        And the fun feature is turned off
        When the user tags both other members in a /battle command
        Then the bot should reply with a message saying that the fun feature is turned off
