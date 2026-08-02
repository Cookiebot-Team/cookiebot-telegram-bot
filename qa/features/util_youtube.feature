# Synced from Cookiebot-QA/features/util_youtube.feature, wording unchanged.
#
# The search itself is a cb-worker job (AGENTS.md §2.4 — an external API call
# is not reply-path work, .specs/features/util_youtube/). qa/test_util_youtube.py
# mocks the gateway->worker queue the same way qa/test_util_everyone.py and
# qa/test_util_calladms.py already do for their own worker halves, so "the bot
# should reply with a link" is verified as "the search job was handed off with
# the right query" -- the actual YouTube call and reply are covered by
# packages/cb-worker/tests/test_youtube_job.py, which this suite does not
# re-run (no worker process exists in this harness).
Feature: allows the user to search for a video on youtube and get the link of the video

    Background:
        Given that the bot is in the group and properly set up

    Scenario: user searches for a video on youtube
        Given that the user is in the group
        When the user sends the command "/youtube how to make a cake"
        Then the bot should reply with a link to a youtube video about how to make a cake
