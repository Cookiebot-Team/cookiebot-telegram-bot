# AUTHORED, not ported. `../Cookiebot-QA/features/` has no scenario for this
# feature at all — docs/site/content/docs/feature-map.mdx §4 already lists
# reverse search among the 20+ v1 features the spec never covered. Every
# assertion below is transcribed from v1's own behaviour
# (`Bot/SocialContent.py:113-142`), not from an intent document.
#
# See .specs/features/x_reverse_search/spec.md for the behaviour contract, and
# in particular D-RS-1: v1 hands SauceNAO a Telegram file URL containing the
# bot token. That is fixed, and the regression test lives at the unit layer
# (packages/cb-worker/tests/test_reverse_search_job.py) where the request body
# is visible.

Feature: Reverse image search for the source of a picture

    Background:
    Given that the bot is in the group and properly set up

    Scenario: The command needs something to search for
        When the user sends /buscarfonte without replying to anything
        Then the bot answers with the "reply an image" instructions

    Scenario: Replying to a message that has no image
        When the user replies /buscarfonte to a plain text message
        Then the bot answers with the "reply an image" instructions

    Scenario: A confident match is reported with its source
        Given the search will find a match
        When the user replies /buscarfonte to a picture
        Then the bot replies with the title, the author and the source link

    Scenario: No match found
        Given the search will find nothing
        When the user replies /buscarfonte to a picture
        Then the bot replies that the image seems to be original

    Scenario: The daily search allowance is spent
        Given the daily search limit has been reached
        When the user replies /buscarfonte to a picture
        Then the bot replies that the daily limit was reached

    Scenario: Utility functions are turned off for the group
        Given that utility functions are disabled for the group
        When the user replies /buscarfonte to a picture
        Then the bot says the utility functions are off
        And no search is performed
