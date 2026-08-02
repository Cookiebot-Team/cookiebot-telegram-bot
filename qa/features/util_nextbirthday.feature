# Synced from Cookiebot-QA/features/util_nextbirthday.feature, wording
# unchanged. No conflict with v1 -- see docs/contracts/util_nextbirthday.md.
Feature: displays the next users to have their birthdays

    Background:
        Given that the bot is in the group and properly set up

    Scenario: user sends the command "/nextbirthday"
        Given that the user is in the group
        When the user sends the command "/nextbirthday"
        Then the bot should reply with a list of the next users to have their birthdays, sorted by date

    # --- Scenario below this line was not in the original Cookiebot-QA
    # spec. Added while porting to v2 to prove the real query end-to-end
    # against a known upcoming birthday, not just the list's shape.
    Scenario: a known upcoming birthday appears on the right day
        Given that a user has a birthday in 2 days
        When the user sends the command "/nextbirthday"
        Then that user's name appears under the 2-day heading
