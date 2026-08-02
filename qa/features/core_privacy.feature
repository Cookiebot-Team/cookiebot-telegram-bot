# Synced from Cookiebot-QA/features/core_privacy.feature.
# Scenarios below "Displaying the privacy politics of the bot" are additions:
# v1 (Miscellaneous.py:60-63, COOKIEBOT.py:195-196) accepts the Portuguese and
# Spanish aliases, the @botname form, and ignores a command addressed at a
# different bot — none of that is in the upstream spec.
#
# The three single-alias additions (Portuguese, Spanish, @CookieMWbot) all
# proved the exact same outcome — a reply containing the privacy text — and
# differed only in the command string, so they are one Scenario Outline below
# rather than three near-identical Scenarios. Two more rows were added that no
# prior scenario covered: an alias combined with the @thisbot suffix
# (`parse_command` strips `@target` before the alias lookup, so this is a real,
# previously-untested code path, not a restatement of an existing case).
#
# "User sends /privacy in a private chat" is also net-new: upstream QA has no
# private-chat scenario for this feature at all, but v1 does answer /privacy
# in a DM (COOKIEBOT.py:87-88, hardcoded English) -- .specs/features/private_dispatch/spec.md.
Feature: Displays the privacy politics of the bot, so that the users can know how their data is being used and protected

    Background:
        Given that the bot is in the group and properly set up

    Scenario: Displaying the privacy politics of the bot
        Given that a user is in the group
        When a user sends the command "/privacy"
        Then the bot should reply with a message containing the privacy politics of the bot

    Scenario Outline: Alias or addressed-command variant still works
        Given that a user is in the group
        When a user sends the command "<command>"
        Then the bot should reply with a message containing the privacy politics of the bot

        Examples:
            | command                   |
            | /privacidade              |
            | /privacidad               |
            | /privacy@CookieMWbot      |
            | /privacidade@CookieMWbot  |
            | /privacidad@CookieMWbot   |

    Scenario: Command addressed at a different bot is ignored
        Given that a user is in the group
        When a user sends the command "/privacy@SomeOtherBot"
        Then the user receives no response

    Scenario: User sends /privacy in a private chat
        When a user sends the command "/privacy" in a private chat with the bot
        Then the bot should reply with the English privacy politics regardless of the sender's language
