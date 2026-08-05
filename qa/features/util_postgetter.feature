# Scenario 1 is synced verbatim from
# ../Cookiebot-QA/features/util_postgetter.feature.
#
# Scenarios 2-4 are AUTHORED, not ported. The QA spec has nothing at all for
# `publisher_ask` — the prompt this feature is mapped to in
# docs/site/content/docs/feature-map.mdx:57 — nor for the `publisher_post` gate
# that decides whether a scheduled post is delivered here in the first place.
# See .specs/features/util_postgetter/spec.md.

Feature: Post getter feature that shares posts forwarded from other groups or channels

    Background:
    Given that the bot is in the group and properly set up

    Scenario: Getter feature is set on the group and user views a post forwarded
        Given that the post forwarding feature is enabled on the group
        And the post is forwarded from another group or channel
        When the user views the post
        Then they should see the original source of the post and any relevant information about it

    Scenario: A channel post auto-forwarded into the group is offered for sharing
        Given that the bot is in the group and properly set up
        When Telegram auto-forwards a linked channel's ad into the group
        Then the bot offers to share it
        And the offer carries an accept and a decline button

    Scenario: The offer is not made when the group turned it off
        Given that the group has turned the sharing offer off
        When Telegram auto-forwards a linked channel's ad into the group
        Then the bot says nothing

    Scenario: A group that opted out of receiving posts receives nothing
        Given that the group has a scheduled post due
        And that the group has turned off receiving posts
        When the delivery sweep runs
        Then nothing is forwarded into the group
        And the scheduled post is dropped
