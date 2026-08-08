# Synced from Cookiebot-QA/features/core_botskins.feature, wording unchanged.
#
# The QA scenarios say a skin "should display the ... skin and provide
# event-specific interactions" without saying what either means concretely, so
# each Then step below is bound to the two things a skin observably *is* in v1
# — its brand name, and the two places `is_alternate_bot` changes behaviour
# (`COOKIEBOT.py:130` and `:143`, see `cb_core/skins.py`). Where QA and v1
# disagree on a name ("Pawsy" vs v1's `pawstralbot` token), both are kept:
# migration 0007 makes the id v1's and the display name QA's.
Feature: Bot skins that customizes the bot to cater to specific events

    Background:
        Given that the bot is in the group and properly set up

    Scenario: Bot skin "Bombot" is applied to the bot for "BrasilFurFest"
        Given that the bot skin "Bombot" is applied to Cookiebot
        And the bot is on the "BrasilFurFest" event group
        When the user interacts with the bot in the group
        Then the bot should display the "Bombot" skin and provide event-specific interactions

    Scenario: Bot skin "Pawsy" is applied to the bot for "Pawstral"
        Given that the bot skin "Pawsy" is applied to Cookiebot
        And the bot is on the "Pawstral" event group
        When the user interacts with the bot in the group
        Then the bot should display the "Pawsy" skin and provide event-specific interactions

    Scenario: Bot skin "Tarinbot" is applied to the bot for "SCFurs"
        Given that the bot skin "Tarinbot" is applied to Cookiebot
        And the bot is on the "SCFurs" event group
        When the user interacts with the bot in the group
        Then the bot should display the "Tarinbot" skin and provide event-specific interactions

    # Not in the QA file. v1's flagship is the only persona that announces
    # itself on joining (`COOKIEBOT.py:130`), which is the other half of what
    # makes a skin observable — and it is the half a scenario about an event
    # skin cannot show.
    Scenario: the flagship announces itself when it is added to a group
        When the flagship bot is added to a group
        Then the bot should post its introduction animation

    Scenario: an event skin joins quietly
        Given that the bot skin "Bombot" is applied to Cookiebot
        When that skin's bot is added to a group
        Then the bot should not post an introduction animation
