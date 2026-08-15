"""The two pieces of text a group owns: its rules and its welcome message.

`group_rules` and `group_welcomes` were read and written by the two gateway
handlers that own the commands, which was right while `/newrules` and
`/newwelcome` were the only ways to set them. The Mini App is a second writer,
and a second copy of the upsert in cb-api is exactly the "second way to do
something that already has one" AGENTS.md §8 forbids — so the SQL moved here
and both surfaces call it.

The handlers keep their private `_fetch_rules` / `_save_welcome` seams as thin
wrappers: unit tests monkeypatch those names, and a refactor that also rewrites
the tests proving the old behaviour proves less than it should.

Both tables are distributed on `group_id` and every statement filters on it, so
each of these is a single-shard router query (AGENTS.md §4).
"""

from __future__ import annotations

import dataclasses
from datetime import datetime

from cb_core import db, groups


@dataclasses.dataclass(frozen=True, slots=True)
class GroupText:
    """A stored body plus its provenance — who last set it, and when.

    The API returns all three; the bot only ever needed `body`, which is why
    the handlers' wrappers still hand back a plain string.
    """

    body: str
    updated_by: int | None
    updated_at: datetime


_SELECT = "SELECT body, updated_by, updated_at FROM {table} WHERE group_id = $1"

_UPSERT = """
INSERT INTO {table} (group_id, body, updated_by, updated_at)
VALUES ($1, $2, $3, now())
ON CONFLICT (group_id) DO UPDATE
SET body = EXCLUDED.body,
    updated_by = EXCLUDED.updated_by,
    updated_at = EXCLUDED.updated_at
"""


async def _get(table: str, group_id: int, *, name: str) -> GroupText | None:
    row = await db.fetchrow(_SELECT.format(table=table), group_id, name=name)
    if row is None:
        return None
    return GroupText(body=row["body"], updated_by=row["updated_by"], updated_at=row["updated_at"])


async def _set(table: str, group_id: int, body: str, updated_by: int | None, *, name: str) -> None:
    # The parent `groups` row is a foreign key of both tables, and an admin can
    # set a group's rules from a private chat with the bot — where nothing has
    # created that row yet. Same reasoning as `group_config.set_config`.
    await groups.ensure(group_id)
    await db.execute(_UPSERT.format(table=table), group_id, body, updated_by, name=name)


async def get_rules(group_id: int) -> GroupText | None:
    return await _get("group_rules", group_id, name="rules_lookup")


async def set_rules(group_id: int, body: str, *, updated_by: int | None = None) -> None:
    """v1's PUT-then-POST-on-404 (`Configurations.py:274-276`), as one upsert."""
    await _set("group_rules", group_id, body, updated_by, name="rules_upsert")


async def get_welcome(group_id: int) -> GroupText | None:
    return await _get("group_welcomes", group_id, name="welcome_lookup")


async def set_welcome(group_id: int, body: str, *, updated_by: int | None = None) -> None:
    """v1's PUT-then-POST-on-404 (`Configurations.py:258-260`), as one upsert."""
    await _set("group_welcomes", group_id, body, updated_by, name="welcome_upsert")


__all__ = [
    "GroupText",
    "get_rules",
    "get_welcome",
    "set_rules",
    "set_welcome",
]
