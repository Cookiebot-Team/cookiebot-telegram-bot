# Synced from ../Cookiebot-QA/features/util_deletereposts.feature, with two
# corrections where the spec describes something v1 does not do. AGENTS.md §1:
# v1 wins for observable behaviour, and the divergence is recorded rather than
# silently picked (docs/site/content/docs/feature-map.mdx).
#
#  1. Scenario 1 said "all posts sent by the post getter feature are deleted".
#     `cancel_posts` (Bot/Publisher.py:322-324) deletes *scheduled, not yet
#     sent* rows and touches no message that already went out. Tightened to
#     "scheduled".
#  2. Scenario 2 asserted 'You don't have permission to use this command or are
#     in anonymous mode' plus a video showing how to leave anonymous mode. That
#     is `/configurar`'s refusal (Configurations.py:139-143), not this
#     command's — v1 answers here with the plain "You are not a group admin!"
#     and sends no video (Publisher.py:319-321).
#
# QA spells the trigger /deletereposts; v1 ships /deleteposts and /apagarposts
# (feature-map.mdx:50). All three resolve — asserted in
# packages/cb-gateway/tests/test_deletereposts.py.

Feature: Delete all posts gotten by the post getter

    Background:
    Given that the bot is in the group and properly set up

    Scenario: Admin uses /deletereposts
        Given the user is an admin on that group
        And the group has scheduled posts
        When they use the /deletereposts command
        Then all scheduled posts requested by that group are deleted
        And the bot confirms with "Posts and reposts canceled!"

    Scenario: user tries to use /deletereposts
        Given that the user is in a group
        When they tried to use the /deletereposts command
        Then the bot should send a message on the group saying "You are not a group admin!"
        And no scheduled posts are deleted
