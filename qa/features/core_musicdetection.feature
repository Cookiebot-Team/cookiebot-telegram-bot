# Authored, not ported. `../Cookiebot-QA/features/` has no music-detection
# feature file. Every step is derived from
# `../COOKIEBOT-Telegram-Group-Bot/Bot/Audio.py:6-20` and its dispatch at
# `COOKIEBOT.py:155-159`.
#
# The recognition itself is a cb-worker job (AGENTS.md §2.4), so what this layer
# proves is that the right voice note crossed the queue -- and, just as
# importantly, that the handler *yields* so the transcribe->AI sub-step v1 runs
# for the same note is still reachable. The recognition, the answer strings and
# the breaker are `packages/cb-worker/tests/test_music_job.py`.
# Contract: docs/contracts/core_musicdetection.md.
Feature: identifying a song from a voice note sent to the group

    Background:
        Given that the bot is in the group and properly set up
        And that music detection is switched on

    Scenario: a voice note is fingerprinted
        When the user sends a voice note
        Then the bot should hand the voice note to the recognition job

    Scenario: an ordinary text message is not
        When the user sends the message "just talking"
        Then the bot should not hand anything to the recognition job

    # v1 runs the music check and the transcribe->AI sub-step from the same
    # `voice` branch (COOKIEBOT.py:156-162), so this handler must never consume
    # the update.
    Scenario: the voice note is still available to the handlers below
        When the user sends a voice note
        Then the update should still reach the handlers registered after it

    Scenario: music detection is switched off
        Given that music detection is switched off
        When the user sends a voice note
        Then the bot should not hand anything to the recognition job

    Scenario: the utility functions are switched off
        Given utility functions are disabled for the group
        When the user sends a voice note
        Then the bot should not hand anything to the recognition job
        And the bot should say nothing at all
