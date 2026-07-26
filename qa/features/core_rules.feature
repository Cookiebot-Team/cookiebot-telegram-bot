# Synced from Cookiebot-QA/features/core_rules.feature.
#
# The "not an admin" scenario below is copied verbatim from upstream, but its
# text and video do not match v1's actual /newrules behaviour (see
# docs/contracts/core_rules.md): that failure message and video are what v1
# shows for /configurar (Configurations.py:141-144, mirrored in
# util_config.feature and core_welcome.feature), not for /newrules.
# GroupShield/Configurations.py:266-283 + COOKIEBOT.py:266-269,293-295 show that
# v1 never gates the /newrules *command* on admin status at all — it always
# replies with the same prompt, and only checks admin status when someone
# replies to that prompt, at which point the real rejection text is the
# hardcoded "You are not a group admin!" (Configurations.py:270-271). This
# repo's pytest-bdd/gherkin/Python combination errors on any `@tag` line during
# collection (a `re.split(..., maxsplit)` DeprecationWarning inside the
# `gherkin` parser library, promoted to an error by this repo's
# `filterwarnings`), so the scenario cannot be tagged `@xfail`; instead its
# steps in qa/test_core_rules.py assert the real, observable v1 behaviour
# (the /newrules prompt, no video) rather than retyping the spec's mismatched
# quoted text — see qa/test_core_rules.py's `bot_says_on_group` and
# `bot_displays_video`. The real non-admin-rejection behaviour (on the reply,
# not the command) is covered by the "user who is not an admin replies to the
# /newrules prompt" scenario added below.
#
# Scenarios below "group admin sets new rules" through "different bot" are
# additions for v1 behaviour the spec missed: the reply-capture mechanics
# themselves (the original spec asserts the admin path in prose but has no
# non-admin-reply scenario), the PT/ES aliases, the anonymous-admin case
# (docs/contracts/admins.md), and a command addressed at a different bot.
#
# The "Given the group already has rules configured" line is the only addition
# to the wording of an *existing* scenario in this file (the original two
# /rules scenarios are otherwise textually identical and only distinguishable
# by their titles — upstream leaves the "rules exist" precondition implicit).
# It is a new line, not a change to any existing line.
#
# The PT/ES alias pairs for /rules and for /newrules, and the single
# "different bot" scenario, were four near-duplicate additions that only ever
# varied the command string for an otherwise identical outcome — each pair (and
# the singleton) is now one Scenario Outline below instead. Each table also
# gained a row for a combination no prior scenario exercised: /rules and
# /newrules addressed at *this* bot (an alias + "@thisbot" combo, alongside the
# plain aliases), and /newrules addressed at a *different* bot (previously only
# /rules had that case). `parse_command` strips `@target` before resolving the
# alias, so these combinations are real, distinct code paths, not restatements.
Feature: rules command that displays the set group rules

    Background:
        Given that the bot is in the group and properly set up

    Scenario: User uses /rules command to view the group rules
        Given the group already has rules configured
        And the user sends the command /rules
        When the bot receives the command
        Then the bot should send a message to the group displaying the set rules for that group

    Scenario: User tries to use /rules command but no rules are set
        Given the user sends the command /rules
        When the bot receives the command
        Then the bot should send a message to the group saying "No rules have been set for this group yet. Please contact an admin to set the rules using /newrules command"

    Scenario: group admin sets new rules using /newrules command
        Given the user sends the command /newrules
        When the user is an admin on that group
        Then the bot should display the message "If you are an admin, REPLY THIS MESSAGE with the message that will be displayed when someone asks for the rules"
        And the admin should be able to reply to the bot's message with the new rules
        And the bot should save the new rules and display a message confirming that the rules have been updated

    Scenario: User tries to use /newrules command but is not an admin
        Given the user sends the command /newrules
        When the user is not an admin on that group
        Then the bot should send a message on the group saying "You don't have permission to use this command or are in anonymous mode"
        And display a video displaying how to remove anonymous mode from the user settings

    Scenario: A user who is not an admin replies to the /newrules prompt
        Given the user sends the command /newrules
        When a user who is not an admin on that group replies to the bot's prompt with new rules text
        Then the bot should send a message on the group saying "You are not a group admin!"

    Scenario: An admin posting anonymously can still set new rules
        Given the user sends the command /newrules
        When an anonymous admin on that group replies to the bot's prompt with new rules text
        Then the bot should save the new rules and display a message confirming that the rules have been updated

    Scenario Outline: Alias or addressed-command variant still displays the group rules
        Given the group already has rules configured
        And the user sends the command <command>
        When the bot receives the command
        Then the bot should send a message to the group displaying the set rules for that group

        Examples:
            | command            |
            | /regras            |
            | /reglas            |
            | /rules@CookieMWbot |

    Scenario Outline: Alias or addressed-command variant still displays the /newrules setup prompt
        Given the user sends the command <command>
        When the bot receives the command
        Then the bot should display the message "If you are an admin, REPLY THIS MESSAGE with the message that will be displayed when someone asks for the rules"

        Examples:
            | command               |
            | /novasregras          |
            | /nuevasreglas         |
            | /newrules@CookieMWbot |

    Scenario Outline: Command addressed at a different bot is ignored
        Given the user sends the command <command>
        When the bot receives the command
        Then the user receives no response

        Examples:
            | command                |
            | /rules@SomeOtherBot    |
            | /newrules@SomeOtherBot |
