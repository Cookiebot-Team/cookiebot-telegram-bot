"""The `groups` row every other table hangs off.

Eight tables carry a foreign key to `groups`: group_configs, group_admins,
group_members, group_rules, group_welcomes, captcha_challenges, media_objects
and llm_usage. Until this module existed nothing created that row at runtime —
the only INSERT lived in the v1 importer — so a group the bot was simply added
to had no parent row, and every one of those writes was rejected:

    insert or update on table "group_configs_102052" violates foreign key
    constraint "group_configs_group_id_fkey_102052"
    DETAIL: Key (group_id)=(-100…) is not present in table "groups_102044".

The bot answered, the menu opened, the button worked, and the value silently
failed to save. A deployment that had never imported v1 data could not
configure a group at all.
"""

from __future__ import annotations

from cb_core import db
from cb_core.logging import get_logger

log = get_logger(__name__)

# Membership is monotonic within a process: once a group's row exists it is
# never removed while the bot is in the chat, so one INSERT per group per
# process is enough and the common path costs a set lookup.
_ensured: set[int] = set()

_UPSERT = """
INSERT INTO groups (group_id, title, chat_type, skin, tenant_id)
VALUES ($1, $2, COALESCE($3, 'supergroup'), COALESCE($4, 'cookiebot'), COALESCE($5, 'cookiebot'))
ON CONFLICT (group_id) DO UPDATE
   SET title = COALESCE(EXCLUDED.title, groups.title)
"""


async def ensure(
    group_id: int,
    *,
    title: str | None = None,
    chat_type: str | None = None,
    skin: str | None = None,
    tenant_id: str | None = None,
) -> None:
    """Make sure `groups` has a row for this chat.

    Idempotent and safe to call on every update. The DO UPDATE only refreshes
    the title — a renamed group should not silently move tenant, and clobbering
    `joined_at` on every message would destroy the only record of when the bot
    arrived.

    Failures are logged, not raised: this runs before the handler, and a write
    that fails here should degrade the features that need the row rather than
    drop the update entirely.
    """
    if group_id in _ensured:
        return
    try:
        await db.execute(
            _UPSERT, group_id, title, chat_type, skin, tenant_id, name="group_ensure"
        )
    except Exception as exc:  # noqa: BLE001 - never let this stop an update
        log.warning("group.ensure_failed", group_id=group_id, error=str(exc))
        return
    _ensured.add(group_id)


def forget(group_id: int) -> None:
    """Drop the memo, so the next update re-runs the upsert. For tests, and for
    the case where the row is deleted underneath a running process."""
    _ensured.discard(group_id)
