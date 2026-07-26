Feature: media restrict feature that prevents new users from posting media in the group

    Background:
        Given that the bot is in the group and properly set up

    Scenario: New user is restricted from posting media
        Given that a new user joins the group
        When the new user attempts to post media content
        Then the bot should prevent the new user from posting media and display a warning message

    Scenario: Existing user is allowed to post media
        Given that an existing user is in the group for more than the time limit set for media restrictions
        When the user attempts to post media content
        Then the bot should allow the existing user to post media without any restrictions

    # --- Scenarios below this line were not in the original Cookiebot-QA spec.
    # Added while porting to v2 to cover v1 behaviour (GroupShield.py:140-152,
    # `configs.timeWithoutSendingImages`) the spec above did not exercise. See
    # docs/contracts/core_mediarestrict.md for the full v1 trace and the v1-vs-v2
    # mechanism comparison (v1 mutes natively at join time; v2 deletes reactively
    # based on `group_members.joined_at`).

    Scenario: Media restriction is disabled for the group
        Given that media restriction is disabled for the group
        And that a new user joins the group
        When the new user attempts to post media content
        Then the bot should allow the new user to post media without any restrictions

    Scenario: An admin is never restricted, even right after joining
        Given that a new user joins the group
        When an admin attempts to post media content
        Then the bot should allow the admin to post media without any restrictions

    Scenario: The warning message states the configured number of minutes
        Given that a new user joins the group
        When the new user attempts to post media content
        Then the warning message states the configured restriction time in minutes

    Scenario Outline: Every kind of restricted content is blocked for a new user
        Given that a new user joins the group
        When the new user attempts to post a <content type>
        Then the bot should prevent the new user from posting media and display a warning message

        Examples:
            | content type |
            | photo        |
            | video        |
            | animation    |
            | sticker      |
