"""x_sticker_autoreply's pool: v1's global sticker `file_id` database

v1: `StickerDatabase.java` (`../COOKIEBOT-backend`) is nothing but
`@Id private String id` — one Mongo collection, no `chat_id`, no per-group
scope at all, holding exactly a Telegram sticker `file_id`
(`add_to_sticker_database`, `../COOKIEBOT-Telegram-Group-Bot/Bot/
SocialContent.py:208-218`). Read back at random by `reply_sticker`
(`SocialContent.py:220-222`) via Mongo's own `$sample` aggregation stage
(`StickerDatabaseService.getRandom`) — unlike `randomdatabase`'s sibling
service, this one never loaded the whole collection into the JVM, so there is
no FEATURE-MAP-flagged defect to fix here.

## Reference table, not distributed — the opposite of `fun_random`'s choice

`fun_random`'s `media_objects` (`0002_media_and_llm_usage.py`) is distributed
on `group_id` and colocated with `groups` specifically because a member's
photo or video posted in one group must never surface in another — that is
private content, and the group is the real unit of ownership. A sticker
admitted to *this* table is not: `add_to_sticker_database`'s own write-side
filters (ported verbatim in `cb_gateway/handlers/sticker_autoreply.py`)
refuse anything without a `set_name` matching `^[a-zA-Z0-9]+$`, i.e. every row
this table ever holds already belongs to a named, published Telegram sticker
set — the same public asset `sendSticker` will deliver into any chat the bot
is a member of, regardless of which group first sent it. The `file_id` alone
identifies nothing about which group posted it, unlike a photo's bytes; there
is no group's content to leak by pooling globally.

Two consequences of that choice, both real costs, not free ones:

1. **The Mongo->Citus importer has nowhere to put a `group_id` even if this
   table wanted one.** `StickerDatabase.java` never recorded one — v1's write
   path takes only the sticker message, never persists `chat_id` alongside
   it. Had this been a per-group table, every migrated row would have been as
   unplaceable as `randomdatabase`'s rows still are; a reference table has no
   such column to fill, so `cb_worker.importer.mappers.map_stickerdatabase`
   can map every document.
2. **A sticker pooled by an NSFW-titled or non-`sfw` group can never be
   scoped away from the rest of the deployment after the fact**, because
   there is no `group_id` to scope by. That makes the write-side filters the
   *only* safety boundary this table has — get them wrong and there is no
   per-group blast radius the way `media_objects` has one. Ported byte for
   byte from v1 for exactly this reason.

Small (one Telegram id per row, never more than v1's own collection ever
held) and read on a passive, best-effort reply path — exactly the shape
`users`/`blacklist`/`bots`/`signing_keys` already share
(`0001_initial_schema.py:136`, `0008_signing_keys.py`).

Revision ID: 0009
Revises: 0008
"""

from __future__ import annotations

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE sticker_pool (
            file_id     text PRIMARY KEY,
            created_at  timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("SELECT create_reference_table('sticker_pool')")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS sticker_pool")
