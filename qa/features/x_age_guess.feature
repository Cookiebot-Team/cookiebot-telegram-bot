# Authored here, not synced: x_age_guess has no scenario in Cookiebot-QA at
# all (it is one of the "shipped in v1, never specified in QA" rows added to
# scripts/spec.py). Behaviour is taken from v1's own code — Miscellaneous.py:
# 185-202 — and its dispatch at COOKIEBOT.py:226-227, not from a spec document.
#
# The no-argument, happy-path and "no data" branches are v1's own three. The
# external-failure scenario has no v1 behaviour to preserve: v1 lets a bad
# response from agify.io propagate uncaught, answering nothing at all. This
# port answers with the same text as "no data" instead — see age.py's module
# docstring, deviation 3.
Feature: Guesses a name's age via agify.io, so that a member can ask "how old is <name>?"

    Background:
        Given that the bot is in the group and properly set up
        And agify.io is stubbed to answer normally

    Scenario Outline: Command sent without a name
        Given that a user is in the group
        When a user sends the command "<command>"
        Then the bot should reply with the usage example

        Examples:
            | command |
            | /age    |
            | /idade  |
            | /edad   |

    Scenario: A name agify.io has data for
        Given that agify.io reports an age of 42 from 12345 records
        When a user sends the command "/age Mekhy"
        Then the bot should reply with the guessed age and sample size

    Scenario: A name agify.io has no data for
        Given that agify.io reports zero records
        When a user sends the command "/age Zzyzx"
        Then the bot should reply that it does not know

    Scenario: Fun functions are switched off for the group
        Given that fun functions are disabled for the group
        When a user sends the command "/age Mekhy"
        Then the bot replies that fun functions are off

    Scenario: agify.io is unreachable
        Given that agify.io is down
        When a user sends the command "/age Mekhy"
        Then the bot should reply that it does not know
