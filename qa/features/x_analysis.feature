# Authored here, not synced: x_analysis has no scenario in Cookiebot-QA at all
# (it is one of the eight "shipped in v1, never specified in QA" rows added to
# scripts/spec.py). Behaviour is taken from v1's own code — Miscellaneous.py:71-81
# and its dispatch at COOKIEBOT.py:202-203 — not from a spec document.
#
# The no-reply branch and the dump branch are v1's two branches. The truncation
# scenario is the one deviation: v1 sent the dump unconditionally and Telegram
# rejected anything over 4096 characters, so the command did nothing at all on
# exactly the large messages worth analysing.
Feature: Dumps a replied-to message's raw Telegram payload, so that a member can report what the bot actually received

    Background:
        Given that the bot is in the group and properly set up

    Scenario: Command sent without replying to anything
        Given that a user is in the group
        When a user sends the command "/analysis"
        Then the bot should reply telling the user to reply to a message

    Scenario Outline: Alias still works
        Given that a user is in the group
        When a user sends the command "<command>" replying to a message
        Then the bot should reply with the replied-to message's fields

        Examples:
            | command              |
            | /analysis            |
            | /analise             |
            | /analisis            |
            | /analysis@CookieMWbot |

    Scenario: The command reacts to itself before answering
        Given that a user is in the group
        When a user sends the command "/analysis" replying to a message
        Then the bot reacts to the command with a thinking face

    Scenario: A payload too large for Telegram is truncated, not dropped
        When the payload to render is longer than a Telegram message allows
        Then the rendered dump fits in one message and says it was truncated
