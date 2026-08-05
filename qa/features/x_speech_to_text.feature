Feature: speech to text -- a voice note becomes text, two ways

    No v1 QA scenario exists for either shape (spec.md: "the only voice-adjacent
    QA file is core_musicdetection.feature", which covers Shazam, a different
    function in the same v1 file, and is out of scope). Shape (a) (the ported
    voice-to-AI sub-step) is authored from spec.md's Phase 2 table and
    design.md's R1; shape (b) (the net-new /transcribe command) is authored
    from spec.md's "Shape (b)" section and design.md's R2, per tasks.md T5.

    Background:
        Given that the bot is in the group and properly set up

    Scenario: A voice note replying to the bot gets an AI reply and no transcript message
        Given the transcript will be "the raw transcript, never shown"
        And the AI will answer with "Sure, here's the deal."
        When the user sends a voice note replying to a message from the bot
        Then the bot replies with "Sure, here's the deal."
        And the transcript itself is never sent to the chat

    Scenario: A voice note that is not a reply to the bot gets nothing
        When the user sends a voice note that is not a reply to anything
        Then the bot sends nothing at all

    Scenario: The fun feature being off is silent for a voice reply too, not a fun_off notice
        Given the fun feature is turned off in the group
        When the user sends a voice note replying to a message from the bot
        Then the bot sends nothing at all
        And the transcript is never generated
        And the model is never asked

    Scenario: /transcribe on a voice reply returns the transcript
        Given the transcript will be "this is what the voice note said"
        When the user replies to a voice note with "/transcribe"
        Then the bot replies to the voice note with "this is what the voice note said"

    Scenario: /transcribe with no voice reply explains itself
        When the user sends "/transcribe" without replying to anything
        Then the bot replies with "reply to a voice message to transcribe it"

    Scenario: An over-length voice note is refused
        When the user sends a voice note over the transcription limit, replying to a message from the bot
        Then the bot replies with "that voice message is too long to transcribe (max 300 seconds)"
        And the transcript is never generated
