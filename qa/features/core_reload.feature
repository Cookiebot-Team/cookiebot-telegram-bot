# Authored here, not synced: Cookiebot-QA has no scenario for /reload. It is in
# v1's dispatcher (COOKIEBOT.py:197-201) and in the help text this repo ships
# verbatim, which is why it has to exist at all — see handlers/reload.py.
Feature: Reloads the group's cached admins and settings, so that a stale answer can be corrected on demand

    Background:
        Given that the bot is in the group and properly set up

    Scenario Outline: The command confirms the reload
        Given that a user is in the group
        When a user sends the command "<command>"
        Then the bot confirms the memory was reloaded

        Examples:
            | command      |
            | /reload      |
            | /recarregar  |

    Scenario: Telegram refuses to hand over the admin list
        Given that Telegram will not return the group's administrators
        When a user sends the command "/reload"
        Then the bot confirms the memory was reloaded
