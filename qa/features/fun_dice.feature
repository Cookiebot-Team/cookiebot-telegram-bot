# Synced from Cookiebot-QA/features/fun_dice.feature. That spec's own trigger
# ("roll 6") has no v1 equivalent at all -- v1 ships /dado, /dice and /d<N>
# (docs/FEATURE-MAP.md's fun_dice row: "spec/code trigger mismatch"). Scenarios
# below "User rolls a die without specifying sides" are additions covering v1's
# real behaviour (Miscellaneous.py:160-183, dispatched from COOKIEBOT.py:248-255)
# that the upstream spec never exercises: see docs/contracts/fun_dice.md for the
# full contract.
Feature: bots rolls an n-sided die and returns the result

    Background:
        Given that the bot is in the group and properly set up

    Scenario: User rolls a 6-sided die
        Given that the user is a member of the group
        When the user sends the command "roll 6"
        Then the bot should respond with a number between 1 and 6

    Scenario: User rolls a 20-sided die
        Given that the user is a member of the group
        When the user sends the command "roll 20"
        Then the bot should respond with a number between 1 and 20

    Scenario: User rolls a die without specifying sides
        Given that the user is a member of the group
        When the user sends the command "roll"
        Then the bot should respond with an error message indicating that the number of sides must be specified

    # --- Scenario Outlines below this line were not in the original
    # Cookiebot-QA spec. Added while porting to v2, then converted to tables
    # during the data-driven pass: each row exercises a distinct branch of
    # dice.py:parse_invocation, not a copy of another row's behaviour.

    Scenario Outline: Equivalent triggers roll the same way, with no upper bound on sides
        Given that the user is a member of the group
        When the user sends the command "<command>"
        Then the bot should respond with a number between <low> and <high>

        Examples:
            | command               | low | high               |
            | /d6                   | 1   | 6                  |
            | roll 999999999999     | 1   | 999999999999       |

    Scenario Outline: The repeat count is clamped between one and twenty rolls
        Given that the user is a member of the group
        When the user sends the command "<command>"
        Then the bot should roll the die <times> times, each result between <low> and <high>

        Examples:
            | command | times | low | high |
            | /d6 3   | 3     | 1   | 6    |
            | /d6 99  | 20    | 1   | 6    |
            | /d6 0   | 1     | 1   | 6    |

    Scenario Outline: Malformed or bare invocations fall back to the usage example instead of going silent
        Given that the user is a member of the group
        When the user sends the command "<command>"
        Then the bot should respond with an error message indicating that the number of sides must be specified

        Examples:
            | command     |
            | /dado       |
            | /dice       |
            | /dado 6     |
            | /d0         |
            | /d6 banana  |
            | roll banana |
            | roll 0      |
            | roll -5     |

    Scenario: Rolling with utility functions turned off for the group
        Given that the user is a member of the group
        And utility functions are disabled for the group
        When the user sends the command "roll 6"
        Then the bot should display a message saying utility functions are disabled

    Scenario: Command addressed at a different bot is ignored
        Given that the user is a member of the group
        When the user sends the command "/dado@SomeOtherBot"
        Then the user receives no response
