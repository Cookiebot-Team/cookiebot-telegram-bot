# Authored, not ported. `../Cookiebot-QA/features/` has no distortion feature
# file — x_distortion is one of the v1 features shipped with no scenario
# anywhere (docs/site/content/docs/feature-map.mdx §4), so AGENTS.md §5 makes
# writing it part of the port. Every step is derived from
# `../COOKIEBOT-Telegram-Group-Bot/Bot/Miscellaneous.py:377-433` and its dispatch
# at `COOKIEBOT.py:242-243`.
#
# The carve and the ffmpeg pass are a cb-worker job (AGENTS.md §2.4), so
# "the bot should distort it" is verified here as "the job was handed off with
# the right file and kind" — `packages/cb-worker/tests/test_distort.py` and
# `test_distortion_job.py` cover the pixels and the sends. Same split
# `qa/test_util_youtube.py` already uses. Contract: docs/contracts/x_distortion.md.
Feature: destroying media that the command replies to

    Background:
        Given that the bot is in the group and properly set up
        And that the user is in the group

    Scenario: the command is sent with no reply and no argument
        When the user sends the command "/destroy"
        Then the bot should explain what to reply to

    Scenario: replying to a photo
        When the user replies to a photo with "/destroy"
        Then the bot should hand the photo to the distortion job

    Scenario: replying to a voice note
        When the user replies to a voice note with "/destroy"
        Then the bot should hand the audio to the distortion job

    Scenario: replying to a static sticker
        When the user replies to a sticker with "/destroy"
        Then the bot should hand the sticker to the distortion job

    # v1 disabled both of these in the handler and left the frame pipeline
    # behind them unreachable (`Miscellaneous.py:395-397,428-430`).
    Scenario: replying to a video
        When the user replies to a video with "/destroy"
        Then the bot should say video distortion is disabled
        And should not hand anything to the distortion job

    Scenario: replying to an animation
        When the user replies to an animation with "/destroy"
        Then the bot should say GIF distortion is disabled
        And should not hand anything to the distortion job

    Scenario: distorting your own profile picture
        Given the user has a profile picture
        When the user sends the command "/destroy pfp"
        Then the bot should hand the profile picture to the distortion job

    # v1 indexes `['photos'][0]` on an empty list here and dies with no reply
    # at all (`:382`).
    Scenario: distorting a profile picture that does not exist
        When the user sends the command "/destroy pfp"
        Then the bot should say a profile picture is needed
        And should not hand anything to the distortion job

    Scenario Outline: every v1 spelling reaches the same handler
        When the user sends the command "<command>"
        Then the bot should explain what to reply to

        Examples:
            | command   |
            | /destroy  |
            | /zoar     |
            | /destruir |

    Scenario: the fun functions are switched off
        Given fun functions are disabled for the group
        When the user sends the command "/destroy"
        Then the bot should say fun functions are off
