Feature: Doomlist feature that prevents user listed on it to join groups with this feature set
    Background:
        Given that the group has the Doomlist feature enabled
        And the bot is properly set with this feature enabled

    Scenario: User on the Doomlist tries to join the group
        Given that the user is listed on the Doomlist
        When they try to join the group with the bot enabled
        Then they should be prevented from joining the group

    # --- Scenario Outlines below this line were not in the original
    # Cookiebot-QA spec. Added while porting to v2 to cover v1 behaviour
    # (GroupShield.py:193-229, COOKIEBOT.py:142) the spec above did not
    # exercise, then converted to tables during the data-driven pass. See
    # docs/contracts/util_doomlist.md for the full v1 trace: v1 actually
    # consults three independent lists, in order (CAS -> local/backend
    # blacklist -> public raid list), and each has its own failure mode.

    Scenario Outline: The ban message names which list matched
        Given <trigger>
        When they try to join the group with the bot enabled
        Then they should be prevented from joining the group
        And the bot should send a message on the group saying "<text>"

        Examples:
            | trigger                                                      | text                                                                                             |
            | that the user is listed on the Doomlist                      | Banned the new user for <b> being reported in other chats </b>                                  |
            | that the user is flagged by the CAS anti-spam service        | Banned the new user for <b> being flagged by the anti-spam system CAS (https://cas.chat/) </b>   |
            | that the user is flagged by the public raid-block service    | Banned the new user for <b> being reported in other chats </b>                                  |
            | that the user's display name contains a forbidden character  | Banned the new user for <b> being reported in other chats </b>                                  |

    Scenario: A user not on any list joins the group normally
        Given that the user is not listed anywhere
        When they try to join the group with the bot enabled
        Then they should not be prevented from joining the group

    Scenario Outline: The local blacklist still decides the outcome when both external services are down
        Given <listed_state>
        And both the CAS anti-spam service and the public raid-block service are down
        When they try to join the group with the bot enabled
        Then <outcome>

        Examples:
            | listed_state                             | outcome                                              |
            | that the user is listed on the Doomlist   | they should be prevented from joining the group      |
            | that the user is not listed anywhere      | they should not be prevented from joining the group  |

    Scenario: The Doomlist feature is disabled for the group
        Given that the group has the Doomlist feature disabled
        And that the user is listed on the Doomlist
        When they try to join the group with the bot enabled
        Then they should not be prevented from joining the group

    Scenario: An existing member adds a listed user instead of them joining themself
        Given that the user is listed on the Doomlist
        When an existing member adds them to the group instead of them joining themself
        Then they should not be prevented from joining the group
