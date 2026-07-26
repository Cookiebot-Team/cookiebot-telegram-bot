Feature: customized welcome message set by the group admins that is sent to new members when they join the group

    Background:
        Given that the bot is in the group and properly set up

    Scenario: Group admin sets a welcome message using /newwelcome command
        Given the user sends the command /newwelcome
        When the user is an admin on that group
        Then the bot should display the message "If you are an admin, REPLY THIS MESSAGE with the message that will be displayed when someone joins the group. You can include <user> to be replaced with the user name"
        And the admin should be able to reply to the bot's message with the new welcome message
        And the bot should save the new welcome message and display a message confirming that the welcome message has been updated

    Scenario: User tries to use /newwelcome command but is not an admin
        Given the user sends the command /newwelcome
        When the user is not an admin on that group
        Then the bot should send a message on the group saying "You don't have permission to use this command or are in anonymous mode"
        And display a video displaying how to remove anonymous mode from the user settings

    Scenario: New member joins the group and receives welcome message
        Given a new member joins the group
        When the bot detects that a new member has joined
        Then the bot should send a message to the group welcoming the new member using the set welcome message.

    # --- Scenarios below this line were not in the original Cookiebot-QA spec.
    # Added while porting to v2 to cover v1 behaviour (GroupShield.py/
    # Configurations.py) the spec above did not exercise. See
    # docs/contracts/core_welcome.md for the full v1 trace, including the
    # documented conflict between the "not an admin" scenario above and v1's
    # actual runtime behaviour (that scenario's exact wording describes
    # /configurar's anonymous-admin defect, not /newwelcome's real reply-time
    # rejection — the step definitions drive the real trigger and assert the
    # scenario's intent rather than its literal copied text).

    Scenario: New member joins a group that never set a custom welcome message
        Given a new member joins the group
        When the bot detects that a new member has joined
        Then the bot should send the default welcome message for the group's language

    # `_substitute_user_tags` (GroupShield.py:38-47, docs/contracts/core_welcome.md's
    # placeholder table) resolves all ten known tags to the exact same value, so
    # this is one Scenario Outline instead of ten near-identical scenarios.
    # `$username` is deliberately not a row here — see the standalone scenario
    # below the table for why it behaves differently.
    Scenario Outline: New member joins and the group's welcome message includes a user placeholder
        Given the group's welcome message is set to "Welcome <tag> to the crew!"
        And a new member without a Telegram username joins the group
        When the bot detects that a new member has joined
        Then the placeholder is replaced with the new member's first name

        Examples:
            | tag         |
            | {user}      |
            | {username}  |
            | {mention}   |
            | $user       |
            | $(user)     |
            | $(username) |
            | <user>      |
            | <username>  |
            | <name>      |

    # $username is excluded from the table above because it does not resolve
    # the same way as the other nine tags: `_substitute_user_tags` checks
    # `$user` before `$username` (GroupShield.py:40, preserved verbatim — see
    # docs/contracts/core_welcome.md, "A second verified placeholder defect"),
    # and `$user` is a literal, undelimited substring of `$username`, so the
    # substitution fires on the shorter tag first and glues the leftover "name"
    # tail onto the result. This is a real, observable v1 defect being
    # preserved, not a data variant of the same behaviour, so it keeps its own
    # scenario with its own (corrupted) expected output rather than being
    # folded into the table and asserted as if it behaved the same.
    Scenario: The $username placeholder collides with $user and is corrupted, not replaced cleanly
        Given the group's welcome message is set to "hi $username!"
        And a new member without a Telegram username joins the group
        When the bot detects that a new member has joined
        Then the welcome message shows the $user/$username collision defect

    Scenario: A non-admin replies to the bot's welcome prompt
        Given the user sends the command /newwelcome
        When a user who is not an admin on that group replies to the bot's prompt with new welcome text
        Then the bot should send a message on the group saying "You are not a group admin!"
        And the welcome message is not updated

    Scenario: Another bot joins the group
        Given a bot account joins the group
        When the bot detects that a new member has joined
        Then the bot should send a message noting a new bot companion was added
        And no welcome message is sent

    Scenario: The bot itself is added to the group
        Given the bot itself is added as a new member
        When the bot detects that a new member has joined
        Then no welcome message is sent to the group

    Scenario: Several users join the group in a single update
        Given three new members join the group in the same update
        When the bot detects that a new member has joined
        Then only the first new member receives the welcome message
