# No upstream Cookiebot-QA scenario exists for this feature -- and could not:
# its triggers are folder names in a private bucket (Miscellaneous.py:23), so
# until that bucket was exported there was not even a command name to write a
# scenario about. 53 folders came out of the export. These scenarios are
# authored locally against v1's behaviour (Miscellaneous.py:145-158) and
# .specs/features/x_custom_commands/spec.md.
Feature: a group can call a picture pool by name

    Background:
        Given that the bot is in the group and properly set up

    Scenario: A member calls a custom command by name
        Given that the user is a member of the group
        When the user types the command "/louie"
        Then the bot should reply with a picture captioned with the pool's name and id

    Scenario: A member asks for one specific picture
        Given that the user is a member of the group
        When the user types the command "/louie 1"
        Then the bot should reply with picture number 1

    Scenario: A member asks for a picture that does not exist
        Given that the user is a member of the group
        When the user types the command "/louie 999"
        Then the bot should send nothing at all

    Scenario: A command with no pool is not a custom command
        Given that the user is a member of the group
        When the user types the command "/nosuchpool"
        Then the bot should send nothing at all

    Scenario: The fun feature is turned off
        Given that the user is a member of the group
        And the fun feature is turned off
        When the user types the command "/louie"
        Then the bot should reply that fun functions are disabled

    Scenario: A brand whose handler pack does not provide custom commands
        Given that the user is a member of the group
        And the brand runs the minimal handler pack
        When the user types the command "/louie"
        Then the bot should send nothing at all
