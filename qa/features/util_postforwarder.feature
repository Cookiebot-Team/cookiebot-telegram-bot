# Scenarios 1-2 are synced from
# ../Cookiebot-QA/features/util_postforwarder.feature, with the mechanism made
# explicit rather than changed. Both spec scenarios read as though forwarding a
# post to the bot delivers it to group b immediately; v1 requires the bot
# owner's approval in between (Bot/Publisher.py:77-92, :230-286) and then
# delivers on a randomised daily schedule (:329-357). The `Then` each spec
# scenario asserts is unchanged — the approval press and the scheduler tick are
# added as `When` steps so the scenario describes what actually happens.
# Recorded in docs/site/content/docs/feature-map.mdx.
#
# Scenarios 3-7 are AUTHORED. The publisher's approval workflow has no Gherkin
# anywhere in ../Cookiebot-QA — only the prose in
# `features/publicador(PTBR).md`, which feature-map.mdx:60 already flags.
# See .specs/features/util_postforwarder/spec.md.

Feature: Post forwarder feature that shares posts to other groups or channels

    Background:
    Given that the bot is in the group and properly set up

    Scenario: Forwarder feature is set on group a and getter feature is set on group b
        Given that the post forwarding feature is enabled on the group a and the getter feature is enabled on group b
        When a post is forwarded from group a to the bot
        And the owner approves the post
        And a day passes and the delivery sweep runs
        Then the group b should receive the forwarded post with the original source and any relevant information about it

    Scenario: Forwarder feature is set up on group a but getter feature is disabled on the group b
        Given that the post forwarding feature is enabled on the group a and the getter feature is disabled on the group b
        When a post is forwarded from group a to the bot
        And the owner approves the post
        And a day passes and the delivery sweep runs
        Then the bot should not forward the post to the group b

    Scenario: Submitting without replying to anything
        When the user sends /divulgar without replying to a message
        Then the bot answers "You need to reply to a message with the command for me to be able to share it!"

    Scenario: Submitting a message that did not come from a channel
        When the user replies /divulgar to an ordinary group message
        Then the bot answers "This message is not from a channel!"

    Scenario: Submitting a channel post with no caption
        When the user replies /divulgar to a channel post with no caption
        Then the bot answers "This ad needs to have a photo, video or GIF"

    Scenario: An approval press from outside the approval chat is ignored
        Given that a post is waiting for approval
        When someone presses approve from an ordinary group
        Then no post is rendered or scheduled

    Scenario: An admin schedules a repost in their own group
        Given the user is an admin on that group
        When they reply /repost 3 to a message
        Then the bot confirms with "Repost scheduled for the group for 3 days!"
        And the group has one scheduled post

    Scenario: A non-admin cannot schedule a repost
        When a plain member replies /repost to a message
        Then the bot answers "You are not a group admin!"
        And the group has no scheduled posts
