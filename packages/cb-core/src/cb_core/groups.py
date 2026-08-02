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

`ensure` is called from the telemetry middleware, which knows the chat an
update arrived in — and that is not always the group being written to. The
config menu is the case that proves it: `/config` is sent in the group, but the
menu, the button and the reply that carries the new value all happen in the
admin's DM, and the group is named by the prompt text. A DM has no group id
(`middlewares._ids` returns 0 for one, deliberately), so nothing in that flow
ensures anything and the write at the end still fails. `ensure_now` is for the
write path: it is what a caller uses when the database has just told it the row
is missing, and unlike `ensure` it raises rather than degrading, because at that
point there is nothing left to degrade to.
"""

from __future__ import annotations

from cb_core import db
from cb_core.logging import get_logger

log = get_logger(__name__)

# Membership is monotonic within a process: once a group's row exists it is
# never removed while the bot is in the chat, so one INSERT per group per
# process is enough and the common path costs a set lookup. Only a caller that
# knew the chat gets memoised — see `_upsert`.
_ensured: set[int] = set()

# `chat_type` and `skin` are refreshed from the *parameters*, not from EXCLUDED:
# EXCLUDED already carries the COALESCEd placeholder, so a caller that passed
# nothing would overwrite a known value with 'supergroup'/'cookiebot'. Reading
# $3/$4 directly keeps "the caller said nothing" distinguishable from "the
# caller said supergroup", which is what lets a row created by a write path
# that knew neither be corrected by the first update that does.
#
# tenant_id is deliberately not refreshed: a renamed or re-skinned group must
# not silently change tenant, and neither must joined_at be clobbered — it is
# the only record of when the bot arrived.
_UPSERT = """
INSERT INTO groups (group_id, title, chat_type, skin, tenant_id)
VALUES ($1, $2, COALESCE($3, 'supergroup'), COALESCE($4, 'cookiebot'), COALESCE($5, 'cookiebot'))
ON CONFLICT (group_id) DO UPDATE
   SET title = COALESCE(EXCLUDED.title, groups.title),
       chat_type = COALESCE($3, groups.chat_type),
       skin = COALESCE($4, groups.skin)
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
        await _upsert(group_id, title=title, chat_type=chat_type, skin=skin, tenant_id=tenant_id)
    except Exception as exc:  # noqa: BLE001 - never let this stop an update
        log.warning("group.ensure_failed", group_id=group_id, error=str(exc))


async def ensure_now(
    group_id: int,
    *,
    title: str | None = None,
    chat_type: str | None = None,
    skin: str | None = None,
    tenant_id: str | None = None,
) -> None:
    """Create the row, ignoring the memo, and raise if that fails.

    The memo says "this process has already created that row", which stops being
    true the moment something deletes it — a restore, a cleanup, an operator
    fixing something by hand. After that, every write against the group is
    rejected for the rest of the process's life and `ensure` never retries,
    because as far as it is concerned the work is done. A caller that has just
    caught a foreign-key violation knows better than the memo does.
    """
    forget(group_id)
    await _upsert(group_id, title=title, chat_type=chat_type, skin=skin, tenant_id=tenant_id)


async def _upsert(
    group_id: int,
    *,
    title: str | None,
    chat_type: str | None,
    skin: str | None,
    tenant_id: str | None,
) -> None:
    await db.execute(_UPSERT, group_id, title, chat_type, skin, tenant_id, name="group_ensure")
    if skin is not None:
        # Only a caller that knew which chat this is gets to end the matter. A
        # write path that ensures a row it has never seen a message from
        # (`group_config.set_config`, driven from a DM) leaves it unmemoised, so
        # the first in-chat update still runs the upsert and corrects the
        # placeholder title, chat_type and skin it had to invent. That costs one
        # extra statement per config write, which is rare, and it is the
        # difference between a row that is right and a row that looks right.
        _ensured.add(group_id)


def forget(group_id: int) -> None:
    """Drop the memo, so the next update re-runs the upsert. For tests, and for
    the case where the row is deleted underneath a running process."""
    _ensured.discard(group_id)
