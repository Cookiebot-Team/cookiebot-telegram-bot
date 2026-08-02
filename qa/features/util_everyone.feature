# Synced from Cookiebot-QA/features/util_everyone.feature.
#
# QA phrases the trigger as "/ping everyone", which has no v1 equivalent at all
# -- v1 ships /everyone and bare @everyone (COOKIEBOT.py:272-273,
# docs/site/content/docs/feature-map.mdx's util_everyone row records the same
# "spec/code trigger mismatch" fun_dice.feature's header documents for its own
# "roll 6" trigger). qa/test_util_everyone.py's step definitions send the real
# v1 trigger "/everyone" to the dispatcher without changing this file's wording.
Feature: allows the admins of a group to ping everyone in the chat

    Background:
        Given that the bot is in the group and properly set up

    Scenario: Admins can use the command to ping everyone in the chat
        Given that the user is an admin of the group
        When an admin sends the command to /ping everyone
        Then all members of the group should receive a notification

    Scenario: Non-admins cannot use the command to ping everyone in the chat
        Given that the user is not an admin of the group
        When a non-admin sends the command to /ping everyone
        Then the bot should respond with a message indicating that they do not have permission to use this command

    # --- Scenario below this line was not in the original Cookiebot-QA spec.
    # Added while porting to v2 to cover the "fewer than two known members" path
    # (everyone_len, UserRegisters.py:107-110) that the upstream spec never
    # exercises.
    Scenario: Fewer than two known members cannot be pinged
        Given that the user is an admin of the group
        And that the group has fewer than two known members
        When an admin sends the command to /ping everyone
        Then the bot should respond with a message indicating that not enough members are known yet
