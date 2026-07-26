# Synced from Cookiebot-QA/features/core_listcommand.feature.
# Scenarios below "User types /commands in a private chat with the bot" are
# additions: v1 (Miscellaneous.py:124-127, COOKIEBOT.py:85-86,276-277) also
# accepts the Portuguese alias /comandos and ignores a command addressed at a
# different bot — none of that is in the upstream spec.
#
# One v1 behaviour is deliberately NOT a scenario here: the fun/utility gates
# (functionsFun/functionsUtility) do not hide the list even when both are off
# (COOKIEBOT.py:276-277 sits outside either gated elif block). Proving that needs
# a real group_configs row with the gates turned off, and this file's harness has
# no database and must not monkeypatch our own code (AGENTS.md §6) — it is
# covered instead in qa/integration/test_command_catalog.py and recorded in
# docs/contracts/core_listcommand.md.
#
# The Portuguese alias and the "different bot" scenarios are each one row of a
# Scenario Outline below rather than a lone scenario, with a second row added
# for a combination no prior scenario exercised: the alias/canonical command
# combined with an @botname suffix (`parse_command` strips `@target` before
# the alias lookup, so `/commands@CookieMWbot` and `/comandos@SomeOtherBot`
# are real, distinct code paths, not restatements of the existing cases).
Feature: Using /commands displays a list with the commands available to the user

    Background:
        Given that the bot is online and operational

    Scenario: User types /commands in the group chat
        Given that the user is a member of the group
        When they type /commands in the group chat
        Then they should see a list of commands available to them

    Scenario: User types /commands in a private chat with the bot
        Given that the user is not a member of any group
        When they type /commands in a private chat with the bot
        Then they should see a list of commands available to them

    Scenario Outline: Alias or addressed-command variant still shows the list in the group chat
        Given that the user is a member of the group
        When they type <command> in the group chat
        Then they should see a list of commands available to them

        Examples:
            | command               |
            | /comandos             |
            | /commands@CookieMWbot |

    Scenario Outline: Command addressed at a different bot is ignored
        Given that the user is a member of the group
        When they type <command> in the group chat
        Then the user receives no response

        Examples:
            | command                |
            | /commands@SomeOtherBot |
            | /comandos@SomeOtherBot |
