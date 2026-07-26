Feature: sents a random media from any group that the bot is in

    Background:
        Given that the bot is in the group and properly set up

    Scenario: User sends the command to get a random media
        Given that the bot has access to media from groups it is in
        When the user sends the command "/random"
        Then the bot should respond with a random media from one of the groups it is in
        And the media should be appropriate for the group it is sent in

    # --- Scenarios below this line were not in the original Cookiebot-QA spec.
    # Added while porting to v2 to cover v1 behaviour (COOKIEBOT.py:213-220,
    # SocialContent.py:191-206) the spec above did not exercise. See
    # docs/contracts/fun_random.md for the full v1 trace, the v1-vs-v2
    # re-architecture (global cross-group pool -> per-group, content-addressed
    # pool), and the parity table.

    Scenario: Fun functions are disabled for the group
        Given that fun functions are disabled for the group
        When the user sends the command "/random"
        Then the bot should display a message saying fun functions are disabled

    Scenario: No media has ever been collected for this group
        Given that the bot has no media collected for this group
        When the user sends the command "/random"
        Then the user receives no response

    # --- Scenario Outline below: converted to a table during the data-driven
    # pass. Same pool (both a safe and an unsafe item) in every row -- the sfw
    # switch is the only thing that varies, and it genuinely changes which
    # items are eligible, which is exactly what a table should prove. The
    # sfw=off row is new coverage: previously only an all-unsafe pool was
    # tested with sfw off (the standalone scenario below this one), never a
    # mixed pool.
    Scenario Outline: Whether unsafe media can be surfaced depends on the group's safe-for-work setting
        Given that the bot has access to both safe and unsafe media from groups it is in
        And <sfw_setting>
        When the user sends the command "/random"
        Then the bot should respond with a random media from one of the groups it is in
        And <expectation>

        Examples:
            | sfw_setting                                   | expectation                                          |
            | the group is configured as safe-for-work      | the media sent is never the unsafe one               |
            | the group is not configured as safe-for-work  | the media sent may be either the safe or unsafe one  |

    Scenario: A group not configured as safe-for-work can still receive media collected before the switch
        Given that the bot has access to unsafe media from groups it is in
        And the group is not configured as safe-for-work
        When the user sends the command "/random"
        Then the bot should respond with a random media from one of the groups it is in
