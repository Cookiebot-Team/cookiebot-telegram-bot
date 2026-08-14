# Synced from Cookiebot-QA/features/fun_partneredcons.feature.
#
# QA lists six triggers and asks only that each sends "a picture of the
# <event>" -- it says nothing about the countdown caption, which is where all
# of v1's actual behaviour is (Miscellaneous.py:261-323). The scenarios below
# the marker are net-new and cover it.
#
# Scenario titles and wording are QA's, verbatim -- including "in in any group"
# and the two events QA calls conventions but its own wording calls events.
# QA's file also has the /fursmeet scenario twice, byte-identical. That is an
# authoring slip rather than a behavioural conflict; it is not reproduced here,
# since a duplicated scenario tests nothing the first one does not.
#
# /trex is in QA and in no v1 code path. It has 67 images in the bucket that v1
# never listed, no date and no caption string anywhere -- so it sends a picture
# and nothing else. See .specs/features/fun_partneredcons/spec.md.
Feature: sends a picture from the partnered cons from a specific command

    Background:
        Given that the bot is in the group and properly set up

    Scenario: User types /bff in any group
        Given that the user is a member of the group
        When the user types the command "/bff"
        Then the bot should send a picture of the "Brasil Fur Fest" convention to the group

    Scenario: User types /patas in any group
        Given that the user is a member of the group
        When the user types the command "/patas"
        Then the bot should send a picture of the "Patas" convention to the group

    Scenario: User types /fursmeet in any group
        Given that the user is a member of the group
        When the user types the command "/fursmeet"
        Then the bot should send a picture of the "Fursmeet" convention to the group

    Scenario: User types /trex in in any group
        Given that the user is a member of the group
        When the user types the command "/trex"
        Then the bot should send a picture of the "Trex Furplayer" event to the group

    Scenario: User types /furcamp in any group
        Given that the user is a member of the group
        When the user types the command "/furcamp"
        Then the bot should send a picture of the "Furcamp" event to the group

    Scenario: User types /pawstral in any group
        Given that the user is a member of the group
        When the user types the command "/pawstral"
        Then the bot should send a picture of the "Pawstral" convention to the group

    # --- Scenarios below this line were not in the original Cookiebot-QA
    # spec. Added while porting to v2 to cover the countdown caption, the
    # "happening now" window, the ungated dispatch and the empty pool.

    Scenario: The picture is captioned with a countdown
        Given that the user is a member of the group
        When the user types the command "/patas"
        Then the picture should carry a countdown caption naming the event

    Scenario: /trex sends a picture with no caption
        Given that the user is a member of the group
        When the user types the command "/trex"
        Then the picture should carry no caption at all

    Scenario: Convention posters ignore the fun and utility switches
        Given that the user is a member of the group
        And the fun feature is turned off
        And the utility feature is turned off
        When the user types the command "/patas"
        Then the bot should still send the picture

    Scenario: The poster pool has never been catalogued
        Given that the user is a member of the group
        And the poster pool is empty
        When the user types the command "/patas"
        Then the bot should send nothing at all
