Feature: sends a fun complaint message and picture to the group when the user types a specific command

    Background:
        Given that the bot is in the group and properly set up
    
    Scenario: User types the complaint command
        Given that the user is a member of the group
        When the user types the command "/complaint"
        Then the bot should send a fun complaint message to the group
        And the bot should send a fun complaint picture to the group 
        And prompt the user to answer the message with a complaint of their own

    Scenario: User responds to the complaint message
        Given that the user has received the fun complaint message
        When the user responds to the message with their own complaint
        Then the bot should send a voice message with a on-hold music to the group
        And then after some minutes answer with a random phrase.
        

        

    # --- Scenarios below this line were not in the original Cookiebot-QA spec
    # (the two scenarios above are synced from
    # Cookiebot-QA/features/fun_complaint.feature, wording unchanged). Added
    # while porting to v2 to cover v1 behaviour (Miscellaneous.py:240-259,
    # dispatched COOKIEBOT.py:215,234-235,300-301) the upstream spec never
    # exercises: the fun-off gate on entry 1, and entry 2's precondition that
    # the replied-to caption actually carries one of the two Milton signatures
    # (D-CP-3, .specs/features/fun_complaint/spec.md).

    Scenario: The fun feature is turned off
        Given the fun feature is turned off
        When the user types the command "/complaint"
        Then the bot should reply with a message saying that the fun feature is turned off
        And the bot sends nothing else

    Scenario: A reply to a photo without either Milton signature does nothing
        Given the user has received a photo with an unrelated caption
        When the user responds to that photo with their own complaint
        Then the user receives no response
