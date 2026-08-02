# Synced from Cookiebot-QA/features/fun_ship.feature.
#
# One Then line is deliberately NOT byte-identical to the upstream spec. Upstream
# says "/shipp @user1" replies "with a shipp of user1 and another user in the
# group". v1 does not do that: `shipp` only reads arguments when the whole
# message splits into three or more tokens (UserRegisters.py:219), so a single
# argument is discarded and both targets come from the random pool. AGENTS.md §1
# gives v1 the call on observable behaviour, so the scenario keeps its name and
# its trigger and states what actually happens. The conflict is recorded in
# docs/contracts/fun_ship.md and docs/site/content/docs/feature-map.mdx.
#
# Scenarios below the marker are additions covering v1 behaviour the upstream
# spec never exercises.
Feature: creates a shipp from one or more users

    Background:
        Given that the bot is in the group and properly set up

    Scenario: Create a shipp from two users
        Given that the group has registered members
        When the user sends the command /shipp
        Then the bot should reply with a shipp of two users in the group

    Scenario: Create a shipp with one user already tagged
        Given that the group has registered members
        When the user sends the command /shipp @user1
        Then the bot should reply with a shipp of two users in the group

    Scenario: Create a shipp with the fun feature turned off
        Given that the group has registered members
        And the fun feature is turned off
        When the user sends the command /shipp @user1
        Then the bot should reply with a message saying that the fun feature is turned off

    # --- Scenarios below this line were not in the original Cookiebot-QA spec.
    # Added while porting to v2 to cover v1 behaviour (UserRegisters.py:216-250,
    # dispatched from COOKIEBOT.py:214-233) the spec above does not exercise.

    Scenario: Two tagged users are shipped verbatim, member or not
        Given that the group has registered members
        When the user sends the command /shipp @stranger_a @stranger_b
        Then the bot should reply with a shipp of stranger_a and stranger_b

    Scenario Outline: Every v1 trigger spelling ships
        Given that the group has registered members
        When the user sends the command <command>
        Then the bot should reply with a shipp of two users in the group

        Examples:
            | command   |
            | /ship     |
            | /shippar  |

    Scenario: A group where nobody but the sender has spoken cannot be shipped
        Given that nobody else in the group has spoken yet
        When the user sends the command /shipp
        Then the bot should reply with a message saying it has not seen enough members

    Scenario: Command addressed at a different bot is ignored
        Given that the group has registered members
        When the user sends the command /shipp@SomeOtherBot
        Then the user receives no response
