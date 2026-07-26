Feature: Setting the language for Cookiebot

    Background:
        Given that the user is on the Cookiebot settings page
        And the user has access to the language settings

    Scenario: User changes the language to Spanish
        Given that the user is on the language settings page
        When they select "Spanish" from the language options
        Then the bot should display texts and respond in Spanish

    Scenario: User changes the language to English
        Given that the user is on the language settings page
        When they select "English" from the language options
        Then the bot should display texts and respond in English

    Scenario: User changes the language to Brazilian Portuguese
        Given that the user is on the language settings page
        When they select "Brazilian Portuguese" from the language options
        Then the bot should display texts and respond in Brazilian Portuguese

    # ------------------------------------------------------------------------
    # The three scenarios above are copied verbatim from
    # ../Cookiebot-QA/features/core_setlang.feature. They describe a **web
    # settings page**; v1 (../COOKIEBOT-Telegram-Group-Bot) has no such surface.
    # v1's actual, observable language-selection behaviour is two things: the
    # in-chat /config menu's Language button (handlers/config_menu.py, owned by
    # another agent, out of scope for this port) and the first-contact
    # derivation below, from the adder's own Telegram language_code when the
    # bot is added to a new group (Configurations.py:242-251, COOKIEBOT.py:133-
    # 134). See docs/contracts/core_setlang.md for the full conflict record.
    # The scenarios below assert that real, in-chat behaviour instead.
    # ------------------------------------------------------------------------

    Scenario: A new group derives its language from the adder's Portuguese Telegram client
        Given a brand new group with no stored language
        When a user whose Telegram client language is "pt-BR" adds the bot to the group
        Then the group's stored language should be "pt"
        And the bot should relabel the group's command menu in Portuguese, Spanish and English scopes

    Scenario: A new group derives its language from the adder's Spanish Telegram client
        Given a brand new group with no stored language
        When a user whose Telegram client language is "es-419" adds the bot to the group
        Then the group's stored language should be "es"

    Scenario: A new group derives its language from the adder's English Telegram client
        Given a brand new group with no stored language
        When a user whose Telegram client language is "en-GB" adds the bot to the group
        Then the group's stored language should be "eng"

    Scenario: A new group with no Telegram language_code at all keeps the default
        Given a brand new group with no stored language
        When a user with no Telegram client language adds the bot to the group
        Then the group's stored language should be left unset

    Scenario: A rejected setMyCommands call does not undo the language change
        Given a brand new group with no stored language
        And Telegram will reject setMyCommands
        When a user whose Telegram client language is "pt-BR" adds the bot to the group
        Then the group's stored language should be "pt"
