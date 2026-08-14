# No upstream Cookiebot-QA scenario exists for this command -- checked against
# the full listing of ../Cookiebot-QA/features/. These scenarios are authored
# locally against v1's behaviour (Miscellaneous.py:137-143) and
# .specs/features/x_drawing_idea/spec.md, not ported from QA.
Feature: sends a random drawing reference with its reference id

    Background:
        Given that the bot is in the group and properly set up

    Scenario Outline: A member asks for a drawing idea
        Given that the user is a member of the group
        When the user types the command "<command>"
        Then the bot should reply with a reference picture captioned with its id

        Examples:
            | command        |
            | /ideiadesenho  |
            | /drawingidea   |
            | /ideadibujo    |

    Scenario: The utility feature is turned off
        Given that the user is a member of the group
        And the utility feature is turned off
        When the user types the command "/drawingidea"
        Then the bot should reply that utility functions are disabled

    Scenario: The reference pool has never been catalogued
        Given that the user is a member of the group
        And the reference pool is empty
        When the user types the command "/drawingidea"
        Then the bot should send nothing at all
