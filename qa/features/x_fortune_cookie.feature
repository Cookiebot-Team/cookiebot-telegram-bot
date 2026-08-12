# Authored here, not synced: x_fortune_cookie has no scenario in Cookiebot-QA
# at all (it is one of the "shipped in v1, never specified in QA" rows added
# to scripts/spec.py). Behaviour is taken from v1's own code —
# Miscellaneous.py:359-375 — and its dispatch at COOKIEBOT.py:240-241.
#
# The one deviation from v1 worth a scenario of its own: v1 blocks the whole
# process for three seconds between sending the animation and deleting it
# (`time.sleep(3)`); this port schedules that same delete-then-answer tail as
# a background task instead of blocking the handler — see fortune.py's module
# docstring, deviation 1. The "sends the fortune" scenario below drives the
# tail synchronously to completion (the test harness's own idiom, same as
# x_fun_complaint's hold).
Feature: Sends an animated fortune cookie with six lucky numbers, so that a member can ask their luck

    Background:
        Given that the bot is in the group and properly set up

    Scenario Outline: The command sends the animation, then the fortune text
        Given that a user is in the group
        When a user sends the command "<command>"
        Then the bot sends the fortune animation
        And the bot eventually deletes the animation and replies with a fortune

        Examples:
            | command       |
            | /sorte        |
            | /fortunecookie |
            | /suerte       |

    Scenario: Fun functions are switched off for the group
        Given that fun functions are disabled for the group
        When a user sends the command "/sorte"
        Then the bot replies that fun functions are off

    Scenario: Every draw of lucky numbers has six distinct-decade numbers
        When the bot draws lucky numbers many times
        Then every draw has six numbers between 1 and 99 from six different tens-decades
