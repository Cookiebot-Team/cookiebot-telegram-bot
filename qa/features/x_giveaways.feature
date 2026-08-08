# Authored, not ported. `../Cookiebot-QA/features/` has no giveaway feature file
# at all — x_giveaways is one of the 20+ v1 features shipped with no scenario
# anywhere (docs/site/content/docs/feature-map.mdx §4), so AGENTS.md §5 says the
# scenario is part of the port. Every step below is derived from
# `../COOKIEBOT-Telegram-Group-Bot/Bot/Giveaways.py` and its dispatch in
# `COOKIEBOT.py:262-263,415-428`, not from a spec.
#
# Two scenarios assert behaviour v1 does *not* have, and are marked as such in
# their own comments: the raffle actually being created (D-GA-1) and an ordinary
# member being able to enter it (D-GA-2). Contract: docs/contracts/x_giveaways.md.
Feature: admins raffle a prize in the group and the bot draws the winners

    Background:
        Given that the bot is in the group and properly set up

    Scenario: an admin starts a giveaway and is asked how many will win
        Given that an admin is in the group
        When the admin sends the command "/giveaway Fursuit of Mekhy"
        Then the bot should ask how many users will be drawn
        And should offer one button per winner count from 1 to 5

    Scenario: a member who is not an admin cannot start a giveaway
        Given that the user is in the group
        When the user sends the command "/giveaway Fursuit of Mekhy"
        Then the bot should say they do not have permission

    Scenario: an admin forgets to say what is being raffled
        Given that an admin is in the group
        When the admin sends the command "/giveaway"
        Then the bot should say what is being raffled must be typed

    # D-GA-1: in v1 this never happened. The prize made a round trip through
    # `json.dumps` -> callback_data -> quote-stripping -> `json.loads`, which
    # raised for every prize that was not a bare JSON literal, so the raffle was
    # never announced and the user saw nothing at all.
    Scenario: picking a winner count announces the giveaway
        Given that an admin is in the group
        When the admin sends the command "/giveaway Fursuit of Mekhy"
        And picks 2 winners
        Then the bot should announce the giveaway naming the full prize
        And should pin the announcement
        And should offer an enter button and an end button

    # D-GA-2: v1 gated *every* GIVEAWAY callback on the admin check
    # (COOKIEBOT.py:416-418), so the "Put me in!" button never worked for the
    # people it invites.
    Scenario: an ordinary member enters the giveaway
        Given that an admin is in the group
        And a giveaway for "Fursuit of Mekhy" with 1 winner is running
        When the user presses the enter button
        Then the bot should confirm they entered the giveaway

    Scenario: entering twice is refused
        Given that an admin is in the group
        And a giveaway for "Fursuit of Mekhy" with 1 winner is running
        When the user presses the enter button
        And the user presses the enter button again
        Then the bot should say they are already participating

    Scenario: ending a giveaway nobody entered
        Given that an admin is in the group
        And a giveaway for "Fursuit of Mekhy" with 1 winner is running
        When the admin presses the end button
        Then the bot should say there were no participants

    Scenario: ending a giveaway draws the winner and offers to draw again
        Given that an admin is in the group
        And a giveaway for "Fursuit of Mekhy" with 1 winner is running
        And the user has entered the giveaway
        When the admin presses the end button
        Then the bot should announce the winner with the prize
        And should ask whether to draw more winners

    Scenario: a member who is not an admin cannot end a giveaway they did not create
        Given that an admin is in the group
        And a giveaway for "Fursuit of Mekhy" with 1 winner is running
        When the user presses the end button
        Then the bot should say only admins can end it

    Scenario: the utility functions are switched off
        Given that an admin is in the group
        And utility functions are disabled for the group
        When the admin sends the command "/giveaway Fursuit of Mekhy"
        Then the bot should say utility functions are off
