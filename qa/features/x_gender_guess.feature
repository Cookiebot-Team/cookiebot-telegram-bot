# Authored here, not synced: x_gender_guess has no scenario in Cookiebot-QA at
# all (it is one of the "shipped in v1, never specified in QA" rows added to
# scripts/spec.py). Behaviour is taken from v1's own code — Miscellaneous.py:
# 204-224 — and its dispatch at COOKIEBOT.py:228-229, not from a spec document.
#
# The no-argument, happy-path and "no data" branches are v1's own three. The
# external-failure scenario has no v1 behaviour to preserve, same reasoning as
# x_age_guess's own. The null-gender scenario is the one v1 could never really
# reach (it should be unreachable behind count == 0) but its own locale data
# already anticipated with a dormant "gender.unknown" catalog entry — see
# gender.py's module docstring, deviation 4, for the full reasoning.
Feature: Guesses a name's gender via genderize.io, so that a member can ask "is <name> a boy or a girl?"

    Background:
        Given that the bot is in the group and properly set up
        And genderize.io is stubbed to answer normally

    Scenario Outline: Command sent without a name
        Given that a user is in the group
        When a user sends the command "<command>"
        Then the bot should reply with the gender usage example

        Examples:
            | command |
            | /gender |
            | /genero |
            | /gênero |

    Scenario: A name genderize.io is confident about
        Given that genderize.io reports "male" with 90% probability from 500 records
        When a user sends the command "/gender Mekhy"
        Then the bot should reply with the guessed gender and probability

    Scenario: A name genderize.io has no data for
        Given that genderize.io reports zero records
        When a user sends the command "/gender Zzyzx"
        Then the bot should reply that it does not know

    Scenario: Fun functions are switched off for the group
        Given that fun functions are disabled for the group
        When a user sends the command "/gender Mekhy"
        Then the bot replies that fun functions are off

    Scenario: genderize.io is unreachable
        Given that genderize.io is down
        When a user sends the command "/gender Mekhy"
        Then the bot should reply that it does not know

    Scenario: genderize.io reports a null gender with a non-zero count
        Given that genderize.io reports a null gender from 3 records
        When a user sends the command "/gender Zyx"
        Then the bot should reply with the unknown-gender text
