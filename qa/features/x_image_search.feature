# No upstream Cookiebot-QA scenario exists for this feature -- checked against
# the full listing of ../Cookiebot-QA/features/. Authored locally against v1's
# behaviour (SocialContent.py:144-170, COOKIEBOT.py:283-289) and
# .specs/features/x_image_search/spec.md.
#
# The feature is v1's catch-all: /qualquercoisa only prints a usage line, and
# the actual search is what happens to *any* command the bot does not know.
# That makes "does not swallow a real command" a behaviour worth a scenario of
# its own -- it is the way this feature breaks every other one.
Feature: an unknown command searches Google Images

    Background:
        Given that the bot is in the group and properly set up

    Scenario: /anything on its own explains itself
        Given that the user is a member of the group
        When the user types the command "/anything"
        Then the bot should reply with the usage example

    Scenario: An unknown command becomes a search
        Given that the user is a member of the group
        When the user types the command "/french fries"
        Then the bot should queue an image search for " french fries"

    Scenario: The search is safe when the group is safe-for-work
        Given that the user is a member of the group
        And the group is configured as safe-for-work
        When the user types the command "/french fries"
        Then the queued search should have safe search on

    Scenario: The search is unfiltered when the group is not safe-for-work
        Given that the user is a member of the group
        And the group is not configured as safe-for-work
        When the user types the command "/french fries"
        Then the queued search should have safe search off

    Scenario: A blocklisted word searches nothing
        Given that the user is a member of the group
        When the user types the command "/etc"
        Then the bot should queue nothing and say nothing

    Scenario: A pasted link is not a search
        Given that the user is a member of the group
        When the user types the command "/see https://example.com"
        Then the bot should queue nothing and say nothing

    Scenario: A command addressed at another bot is not a search
        Given that the user is a member of the group
        When the user types the command "/cat@SomeOtherBot"
        Then the bot should queue nothing and say nothing

    Scenario: A real command is never turned into a search
        Given that the user is a member of the group
        When the user types the command "/isalive"
        Then the bot should answer the real command and queue nothing

    Scenario: The daily limit is reached
        Given that the user is a member of the group
        And the user has used up today's image searches
        When the user types the command "/french fries"
        Then the bot should reply that the image search limit is reached

    Scenario: The utility feature is turned off
        Given that the user is a member of the group
        And the utility feature is turned off
        When the user types the command "/french fries"
        Then the bot should queue nothing and say nothing
