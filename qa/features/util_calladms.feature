Feature: command /adm that ping all adm's from a group

    Background:
        Given that the bot is in the group and properly set up

    Scenario: User pings the bot with the /adm command
        Given that there are admins in the group
        When the user sends the /adm command to the bot
        And confirms the intention to ping all admins
        Then the bot should respond by pinging all admins in the group
        And should send a message on the adm's DM confirming that they have been pinged in a group

    # v1 dispatches /adm, /admin and /report to the exact same confirmation flow
    # (COOKIEBOT.py:274-275) — /report here is *not* the "report this account as
    # spam/fake" feature, it is an alias for calling the admins, same as /admin.
    # The bare @admin/@adm mention forms (calladms.py's _MENTION_TRIGGER regex)
    # reach the same flow too — added as table rows during the data-driven pass.
    Scenario Outline: Alternate triggers reach the same confirmation flow
        Given that there are admins in the group
        When the user sends the <command> command to the bot
        And confirms the intention to ping all admins
        Then the bot should respond by pinging all admins in the group

        Examples:
            | command |
            | /admin  |
            | /report |
            | @admin  |
            | @adm    |

    Scenario: User declines the confirmation
        Given that there are admins in the group
        When the user sends the /adm command to the bot
        And declines the intention to ping all admins
        Then the bot should cancel the request without pinging anyone

    # v1's confirmation button expires 600 seconds after it was sent
    # (COOKIEBOT.py:401) and tells the presser to just run /adm again.
    Scenario: The confirmation has gone stale
        Given that there are admins in the group
        When the user sends the /adm command to the bot
        And confirms the intention more than 10 minutes later
        Then the bot should tell the user the confirmation is too old
        And should not ping anyone
