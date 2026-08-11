# Authored here, not synced: x_unearth has no scenario in Cookiebot-QA — it is
# one of the eight "shipped in v1, never specified in QA" rows. Behaviour comes
# from Miscellaneous.py:325-333 and its dispatch at COOKIEBOT.py:236-237.
#
# The retry scenario is the one deviation from v1 and the reason it is spelled
# out here: v1 wrote `for _ in range(100)` and then `return None` inside the
# except, so it makes exactly one attempt and answers nothing whenever that id
# is a deleted or unforwardable message.
Feature: Forwards a random earlier message from the group, so that old conversations resurface

    Background:
        Given that the bot is in the group and properly set up

    Scenario Outline: The command forwards an earlier message
        Given that a user is in the group
        When a user sends the command "<command>"
        Then the bot forwards a message from the same group

        Examples:
            | command       |
            | /unearth      |
            | /desenterrar  |

    Scenario: Fun functions are switched off for the group
        Given that fun functions are disabled for the group
        When a user sends the command "/unearth"
        Then the bot replies that fun functions are off

    Scenario: The first candidate is gone, a later one is not
        Given that the first message the bot tries to forward no longer exists
        When a user sends the command "/unearth"
        Then the bot forwards a message from the same group

    Scenario: Every candidate is gone
        Given that no message the bot tries to forward still exists
        When a user sends the command "/unearth"
        Then the user receives no response

    Scenario: The candidate id is drawn from the whole history
        When the bot picks a candidate below message id 500
        Then the candidate is between 1 and 500
