# Synced from Cookiebot-QA/features/fun_firecracker.feature, wording unchanged.
# The second scenario below was not in the original spec -- added while porting
# to v2 for the fun-off gate (spec.md success criterion 3; AGENTS.md §6: "write
# the scenario as part of the port"). v1 tells the user, it does not go silent
# (notify_fun_off, Miscellaneous.py:129-131).
Feature: sends a firecracker message sequence to the group when the user types a specific command

    Background:
        Given that the bot is in the group and properly set up

    Scenario: User types the firecracker command
        Given that the user is a member of the group
        When the user types the command "/firecracker"
        Then the bot should send multiple firecracker messages in a sequence to the group

    # --- Scenario below this line was not in the original Cookiebot-QA spec.
    Scenario: The fun feature is turned off
        Given that the user is a member of the group
        And the fun feature is turned off
        When the user types the command "/firecracker"
        Then the bot should reply with a message saying that the fun feature is turned off
