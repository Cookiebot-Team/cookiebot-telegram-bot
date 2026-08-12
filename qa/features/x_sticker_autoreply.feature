# Authored here, not synced: x_sticker_autoreply has no scenario in
# Cookiebot-QA at all (it is one of the "shipped in v1, never specified in
# QA" rows in scripts/spec.py). Behaviour comes from v1's own code —
# add_to_sticker_database/reply_sticker, SocialContent.py:208-222 — and its
# three dispatch sites at COOKIEBOT.py:174-184.
#
# Needs a real database: the pool this feature reads and writes,
# `sticker_pool`, is a real Postgres reference table (migration
# 0009_sticker_pool) with no in-process fallback the way group_config has
# one — a scenario that invented a pooled sticker with nothing behind it
# would prove nothing about the actual write/read path.
#
# The reply-target scenarios use `_is_reply_to_bot`'s fix (deviation 1 in
# sticker_autoreply.py's own docstring): v1 compared the replied-to sender's
# first_name to the literal string "Cookiebot", which never matches any other
# persona this codebase ships. This suite's mock bot answers to the real
# identity check (reply_to_message.from_user.id == bot.id) instead.
Feature: Pools stickers from sfw groups and replies with one at random to a reply aimed at the bot

    Background:
        Given that the bot is in the group and properly set up

    Scenario: A sticker sent in an sfw group is added to the pool
        Given the group is configured sfw
        When a user sends a sticker from a clean, alphanumeric pack
        Then the sticker is added to the pool

    Scenario: A sticker sent in an nsfw-titled group is not added to the pool
        Given the group's title flags it as NSFW
        When a user sends a sticker from a clean, alphanumeric pack
        Then the sticker is not added to the pool

    Scenario: A sticker with a banned emoji is not added to the pool
        Given the group is configured sfw
        When a user sends a sticker whose emoji is on the banned list
        Then the sticker is not added to the pool

    Scenario: Replying to the bot with a sticker gets a random sticker back
        Given the pool already has a pooled sticker
        When a user replies to the bot with a sticker
        Then the bot replies with a sticker from the pool

    Scenario: Replying to another user does not get a sticker back
        Given the pool already has a pooled sticker
        When a user replies to another user with a sticker
        Then the user receives no sticker reply

    Scenario: Fun functions are switched off for the group
        Given the pool already has a pooled sticker
        And that fun functions are disabled for the group
        When a user replies to the bot with a sticker
        Then the user receives no sticker reply
