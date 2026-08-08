# Authored, not ported. `../Cookiebot-QA/features/` has no meme feature file.
# Every step is derived from
# `../COOKIEBOT-Telegram-Group-Bot/Bot/SocialContent.py:224-277` and its dispatch
# at `COOKIEBOT.py:222-223`.
#
# The template fetch, the profile-photo downloads and the compositing are a
# cb-worker job (AGENTS.md §2.4), so this layer proves the reply-path decisions
# and the hand-off; `packages/cb-worker/tests/test_meme_job.py` covers the
# pixels and `packages/cb-core/tests/test_meme_templates.py` the selection
# rules. Contract: docs/contracts/fun_meme.md.
Feature: building a meme from members' profile pictures

    Background:
        Given that the bot is in the group and properly set up
        And that the user is in the group

    Scenario: a plain /meme picks the members itself
        When the user sends the command "/meme"
        Then the bot should hand the meme to the compositing job with no tags

    Scenario: tagging members puts them in the meme
        When the user sends the command "/meme @alice @bob"
        Then the bot should hand the meme to the compositing job tagging alice and bob

    Scenario: more than five members is refused
        When the user sends the command "/meme @a @b @c @d @e @f"
        Then the bot should say more than five members is not possible
        And should not hand anything to the compositing job

    Scenario: the fun functions are switched off
        Given fun functions are disabled for the group
        When the user sends the command "/meme"
        Then the bot should say fun functions are off
        And should not hand anything to the compositing job
