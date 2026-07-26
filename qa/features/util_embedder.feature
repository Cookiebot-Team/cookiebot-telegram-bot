# Synced from Cookiebot-QA/features/util_embedder.feature.
Feature: embedder feature that bypass the need to access a social media or site to see a video or picture

    Scenario: User sends a video link from a social media (e.g. bluesky)
        Given that the bot is running and responsive
        When the user sends a video link from bluesky
        Then the bot should reply to the link with an embedded version of it

    Scenario: User sends an invalid link or a link for an unsupported social media
        Given that the bot is running and responsive
        When the user sends an invalid link
        Then the bot should not respond

    # --- Added: real v1 behaviour (Bot/SocialContent.py:49-84, Bot/COOKIEBOT.py:309-312)
    # the spec above never covers. See docs/contracts/util_embedder.md Phase 3
    # for why each was added. Converted to tables during the data-driven pass
    # where the rows share one behaviour and differ only in the link.

    Scenario Outline: Each rewritten host maps to its own embed domain
        Given that the bot is running and responsive
        When the user sends a link from "<link>"
        Then the bot should reply with "<target>"

        Examples:
            | link                                                         | target                                                        |
            | https://x.com/someuser/status/1234567890123                  | https://fixupx.com/someuser/status/1234567890123              |
            | https://twitter.com/someuser/status/1234567890123            | https://fixupx.com/someuser/status/1234567890123              |
            | https://www.tiktok.com/@someuser/video/7123456789012345678   | https://vm.vxtiktok.com/@someuser/video/7123456789012345678   |
            | https://bsky.app/profile/alice.bsky.social/post/3jt6vw       | https://fxbsky.app/profile/alice.bsky.social/post/3jt6vw      |

    Scenario: User sends a message containing several embeddable links
        Given that the bot is running and responsive
        When the user sends a message with links from both twitter and bluesky
        Then the bot should reply with an embedded version of each link

    Scenario: User sends a link already in embedded form
        Given that the bot is running and responsive
        When the user sends a link that is already an embedded form
        Then the bot should not respond

    Scenario Outline: A host find_embeddable_links detects but v1 never rewrites stays silent
        Given that the bot is running and responsive
        When the user sends a link from "<link>"
        Then the bot should not respond

        Examples:
            | link                                                |
            | https://instagram.com/p/Cabc123XYZ/                 |
            | https://reddit.com/r/aww/comments/abc123/cute_cat    |
            | https://pixiv.net/en/artworks/12345678               |
            | https://e621.net/posts/1234567                       |
            | https://furaffinity.net/view/12345678/               |

    Scenario: User sends a link inside an ordinary sentence, not on its own
        Given that the bot is running and responsive
        When the user sends a bluesky link surrounded by other words
        Then the bot should reply to the link with an embedded version of it

    Scenario: User sends a command that happens to contain a link
        Given that the bot is running and responsive
        When the user sends a command containing a video link from bluesky
        Then the bot should not respond

    Scenario: The group has the utility feature area disabled
        Given that the bot is running and responsive
        And the group has utility functions disabled
        When the user sends a video link from bluesky
        Then the bot should not respond
