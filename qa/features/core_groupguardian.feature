Feature: Group Guardian with captcha anti-bot invasion

        Background:
        Given that the group is protected by Cookiebot
        And the bot is properly set with this feature

    Scenario: User encounters the group and tries to join it
        Given that the user is not a member of the group
        And the user tries to join the group
        When they solve the captcha challenge
        Then they should be able to join the group successfully

    Scenario: User encounters the group and tries to join it but fails the captcha challenge
        Given that the user is not a member of the group
        And the user tries to join the group
        When they fail to solve the captcha challenge correctly or timeouts
        Then they should not be able to join the group

    # --- Scenarios below this line were not in the original Cookiebot-QA spec.
    # Added while porting to v2 to cover v1 behaviour (GroupShield.py:231-345,
    # COOKIEBOT.py:147-148,298-316,391-395) the two scenarios above did not
    # exercise. See docs/contracts/core_groupguardian.md for the full v1 trace.

    Scenario: User answers the captcha wrong once but still has attempts left
        Given that the user is not a member of the group
        And the user tries to join the group
        When they answer the captcha challenge incorrectly
        Then they are told the password is incorrect and are not kicked yet

    Scenario: User exhausts every attempt at the captcha
        Given that the user is not a member of the group
        And the user tries to join the group
        When they answer the captcha challenge incorrectly 5 times
        Then they should not be able to join the group

    Scenario: An admin approves a newcomer through the captcha
        Given that the user is not a member of the group
        And the user tries to join the group
        When an admin presses the approve button
        Then they should be able to join the group successfully

    Scenario: A newcomer cannot approve themselves through the admin button
        Given that the user is not a member of the group
        And the user tries to join the group
        When the newcomer presses the admin-only approve button
        Then they should not be able to join the group

    Scenario: Someone else invites the new member instead of them joining themselves
        Given that the user is not a member of the group
        And an existing member adds the user to the group
        Then no captcha challenge is shown to the user

    Scenario: The captcha feature is turned off for the group
        Given that the user is not a member of the group
        And the group has the captcha feature disabled
        And the user tries to join the group
        Then no captcha challenge is shown to the user

    Scenario: The bot is not an admin of the group
        Given that the user is not a member of the group
        And the bot is not an admin of the group
        And the user tries to join the group
        Then no captcha challenge is shown to the user
