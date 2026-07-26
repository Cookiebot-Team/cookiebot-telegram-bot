Feature: /config command that allows the Admins to configure the bot's settings and preferences

    Scenario: Admin uses /config command to change the bot's language setting
        Given the user sends the command /config
        When the user is an admin on that group
        Then the bot should send a message on the group warning the admin to check their dms
        And the bot should send a message to the user dm's with the configuration options

    Scenario: User tries to use /config command but is not an admin
        Given the user sends the command /config
        When the user is not an admin on that group
        Then the bot should send a message on the group saying "You don't have permission to use this command or are in anonymous mode"
        And display a video displaying how to remove anonymous mode from the user settings

    Scenario: Admin using anonymous mode uses /config command
        Given the user sends the command /config
        When the user is an anonymous admin on that group
        Then the bot should not send the permission denied message
        And the bot should not display the anonymous mode tutorial video
        And the bot should tell the admin it could not reach them privately

    # --- Scenario Outline below: added while porting. QA's own single scenario
    # ("Fun Functions") only proved the menu could answer *one* button; v1's
    # menu has thirteen (config_menu.py's CONFIG_FIELDS, Configurations.py:150-163),
    # each writing a different group_configs column. Converted to a table
    # during the data-driven pass, one row per button, tying its callback
    # letter to the column it actually writes.
    Scenario Outline: Admin presses a button on the config menu
        Given the admin has opened the /config menu in their private chat
        When the admin presses the button for "<label>"
        Then the bot should answer the callback query
        And the bot should prompt for the new value in the private chat
        And the button writes the "<column>" setting when answered

        Examples:
            | label                   | column                  |
            | Language                | language                |
            | FurBots                 | allow_furbots           |
            | Stickers limit          | sticker_spam_limit      |
            | 🕒 Limbo                | media_restrict_seconds  |
            | 🕒 CAPTCHA              | captcha_timeout_seconds |
            | Fun Functions           | functions_fun           |
            | Utility Functions       | functions_utility       |
            | SFW Chat                | sfw                     |
            | Publisher Post          | publisher_post          |
            | Publisher Ask           | publisher_ask           |
            | Thread Posts            | thread_posts            |
            | Max Posts               | max_posts               |
            | Publisher Members Only  | publisher_members_only  |
