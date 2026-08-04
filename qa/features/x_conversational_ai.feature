Feature: conversational AI -- a mention or a reply to the bot gets an in-character answer

    No v1 QA scenario exists for this feature (spec.md "QA -- authored, not
    ported": confirmed against the full ../Cookiebot-QA/features/ listing).
    The scenarios below are authored directly against
    .specs/features/x_conversational_ai/spec.md's own QA section and
    design.md's R3/R4/R5 gates, per tasks.md T7.

    Background:
        Given that the bot is in the group and properly set up

    Scenario: A mention triggers a reply
        Given the AI will answer with "Sure, here's the deal."
        When the user sends a message mentioning the bot
        Then the bot replies with "Sure, here's the deal."
        And the model was asked exactly 1 time

    Scenario: A reply to a bot message triggers a reply
        Given the AI will answer with "Noted."
        And the bot has already sent a plain message in the group
        When the user replies to that bot message with unrelated text
        Then the bot replies with "Noted."

    Scenario: The fun feature being off is silent, not a fun_off notice
        Given the fun feature is turned off in the group
        When the user sends a message mentioning the bot
        Then the bot sends nothing at all
        And the model is never asked

    Scenario: An empty stripped message gets a bare question mark and no model call
        When the user sends a message that is only the bot's name
        Then the bot replies with "?"
        And the model is never asked

    Scenario: An earlier branch wins over the AI branch
        Given the bot has prompted for new rules
        When a non-admin replies to that prompt with text that also mentions the bot
        Then the bot refuses for lack of admin rights
        And the model is never asked

    Scenario: The per-user counter silences the bot after seven consecutive triggers
        Given the AI will answer with "ok"
        When the user mentions the bot 7 times in a row
        Then the bot replies to the first six mentions
        And the seventh mention gets no reply at all
        And the model was asked exactly 6 times

    Scenario: An ordinary message replenishes the exhausted per-user counter
        Given the user's AI streak is fully spent
        When the user sends an ordinary message that does not mention the bot
        Then the user's AI streak has grown by one
