# Synced from Cookiebot-QA/features/util_birthday.feature, wording unchanged.
#
# QA's one scenario is a bare "/birthday" expecting a montage. v1 does not do
# that: birthday()'s very first check (Birthdays.py:16-18) is "if
# manual_chat_id and len(msg['text'].split()) == 1: reply bday.title;
# return" -- a bare /birthday always hits this branch and asks the caller to
# type usernames, never looking up who actually has a birthday. Only
# "/birthday <anything else>" (a second token, @-prefixed or not) reaches the
# real lookup. Per AGENTS.md (v1 code wins for observable behaviour, QA wins
# for intent, conflicts recorded rather than silently resolved), the step
# below asserts what v1 actually does for the bare case; the scenario after
# it is net-new, covering the real montage path QA's wording seems to have
# intended. See docs/contracts/util_birthday.md.
#
# The real collage is a cb-worker job (image compositing, AGENTS.md §2.4) --
# qa/test_util_birthday.py mocks the gateway->worker queue the same way
# qa/test_util_everyone.py/qa/test_util_calladms.py/qa/test_util_youtube.py
# already do for their own worker halves.
Feature: sends a montage of users that has their birthday on that day

    Background:
        Given that the bot is in the group and properly set up

    Scenario: user sends the command "/birthday"
        Given that the user is in the group
        When the user sends the command "/birthday"
        Then the bot should reply with a montage of users that has their birthday on that day

    # --- Scenario below this line was not in the original Cookiebot-QA
    # spec. Added while porting to v2 to cover the shape that actually
    # produces a montage in v1: a second argument, not a bare command.
    Scenario: user sends "/birthday" with a tagged name
        Given that the user is in the group
        When the user sends the command "/birthday @someone"
        Then the bot should enqueue the birthday collage job

    Scenario: the fun feature is turned off
        Given that the user is in the group
        And the fun feature is turned off
        When the user sends the command "/birthday @someone"
        Then the bot should reply with a message saying that the fun feature is turned off
