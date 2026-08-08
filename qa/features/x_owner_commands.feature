# Authored, not ported. `../Cookiebot-QA/features/` has no owner-commands file
# — every step is derived from the owner branch of v1's private-chat block,
# `../COOKIEBOT-Telegram-Group-Bot/Bot/COOKIEBOT.py:83-105`.
#
# The /broadcast fan-out is a cb-worker job (AGENTS.md §2.4), so this layer
# proves the hand-off; `packages/cb-worker/tests/test_broadcast_job.py` covers
# the fan-out itself. Contract: docs/contracts/x_owner_commands.md.
Feature: owner-only operations from the bot's private chat

    Background:
        Given that the bot is running

    Scenario: the owner lists the groups the bot is in
        Given that the sender is the bot owner
        When the owner sends "/grupos" in a private chat
        Then the bot should list the groups and their total

    Scenario: someone who is not the owner gets no answer
        Given that the sender is not the bot owner
        When that user sends "/grupos" in a private chat
        Then the bot should say nothing

    Scenario: the owner blacklists a user
        Given that the sender is the bot owner
        When the owner sends "/blacklist 424243" in a private chat
        Then the bot should confirm the user was blacklisted

    Scenario: the owner removes a user from the blacklist
        Given that the sender is the bot owner
        And the user 424243 is blacklisted
        When the owner sends "/unblacklist 424243" in a private chat
        Then the bot should confirm the user was unblacklisted

    # v1 answers the same line whether or not anything was removed, so an
    # owner could not tell a typo from a successful removal.
    Scenario: unblacklisting someone who was never listed says so
        Given that the sender is the bot owner
        When the owner sends "/unblacklist 999111" in a private chat
        Then the bot should say the user was not blacklisted

    Scenario: the owner broadcasts a message to every group
        Given that the sender is the bot owner
        When the owner sends "/broadcast hello everyone" in a private chat
        Then the bot should queue the broadcast

    Scenario: a broadcast with no message is refused
        Given that the sender is the bot owner
        When the owner sends "/broadcast" in a private chat
        Then the bot should explain how to use it

    # Deliberately not ported — see the handler's module docstring. Answering
    # matters: an owner who types /stop and gets silence assumes it worked.
    Scenario Outline: process control is refused, not silently missing
        Given that the sender is the bot owner
        When the owner sends "<command>" in a private chat
        Then the bot should explain that process control is the orchestrator's job

        Examples:
            | command  |
            | /stop    |
            | /restart |
