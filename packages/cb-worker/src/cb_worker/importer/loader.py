"""Batched, idempotent upserts into Citus — the last of the three importer layers.

`TABLE_LOADS` is the only place that knows the SQL shape of a target table: its
columns, its natural key, and which columns a re-run is allowed to overwrite.
That last part is the idempotency contract this module exists to keep, so it is
worth stating once, here, rather than at each call site:

* `group_configs`, `group_rules`, `group_welcomes`, `group_admins`, `users`: every
  column with a real v1-sourced value is in `update_columns`. Until cutover, v1
  is the sole writer of these fields (a group's language, its rules text, a
  user's Telegram profile), so a re-run reasserting v1's current value is
  exactly the "catch the delta" behaviour `importer/__init__.py` asks for — it
  is never clobbering a v2-side edit because v2 does not yet let anyone make
  one.
* `sticker_spam_window_s` and `doomlist_enabled` are the one exception within
  `group_configs`: `mappers.map_configs` puts a value for both in every row
  (v1's true default, since `Config.java` has neither field at all), but that
  value is not migrated data, it is a constant — so it is in `columns` (a
  first-ever insert should get the right default, same as if the row did not
  exist yet) but deliberately **not** in `update_columns`: a re-run must not
  stomp a value the bot itself changed since, because the import has no
  genuine v1 signal to reassert.
* `groups`: only `title` and `image_url` are overwritten. `chat_type`, `skin`,
  `joined_at`, `left_at`, `username` and `tenant_id` are v2-owned lifecycle
  state with no Mongo source (a `groups` document carries no chat type, no
  skin, no join/leave timestamps, no username), so they are not even in
  `columns` — an import that ran `ON CONFLICT ... DO UPDATE SET left_at = NULL`
  would resurrect a group the gateway had already recorded as departed.
* `blacklist`: `kind`/`reason`/`source` are all in `columns` (the mapper derives
  a real `kind` from the id's sign, and an honest `reason=NULL`/`source='manual'`
  for the rest — see `mappers.map_blacklist`), but none are in `update_columns`.
  `kind` is a no-op to reassert (an id's sign never changes) so leaving it out
  costs nothing; `reason`/`source` are the mapper's best guess, not real data,
  and a v2-side moderator who later annotated a blacklist entry must keep that
  annotation across a re-run.
* `sticker_pool`: `file_id` is both the only column and the conflict key, so
  there is nothing left for `update_columns` to own — a re-run of a row
  already present is a true no-op (`DO NOTHING`), never a reassertion of any
  field, because the whole row *is* the natural key (`mappers.map_stickerdatabase`).
* `users.created_at`, `groups.joined_at` and `blacklist.created_at` are "when we
  first saw this" — deliberately absent from `columns` entirely, so Postgres's
  own `DEFAULT now()` fires once, on the row's real first insert, and a re-run
  never touches it.
* `group_configs.updated_at`, `group_rules.updated_at`, `group_welcomes.updated_at`,
  `group_admins.synced_at` and `users.updated_at` are the opposite: "when we
  last touched this", which an import legitimately owns every time it runs.
  `_STAMP_COLUMN` appends one `now()` value per `load_rows` call — bound once in
  Python and reused for every batch of that call, never `now()` inside the SQL's
  `DO UPDATE SET` — because Citus rejects a non-IMMUTABLE function there (each
  shard would evaluate its own); `group_config.set_config` already applies the
  same fix to this same table (`cb_core/group_config.py:260-263`).

Every row tuple a mapper hands to `MappedRows.add(table, row)` must be
positional and, for a stamped table, one element *shorter* than
`TABLE_LOADS[table].columns` — `load_rows` appends the timestamp itself. By
convention (and because `runner.py`'s FK-stub step depends on it) the
distribution/primary key column — `group_id`, or `user_id`/`subject_id` for the
two reference tables — is always column 0 of the tuple a mapper produces.

Every `TableLoad.conflict_columns` includes the table's own primary key. For the
distributed tables that key already *is* `group_id` (or `(group_id, user_id)`),
which happens to satisfy Citus's rule that the distribution column appear in
every unique constraint (AGENTS.md §4.3) — nothing extra was added to satisfy
Citus, the natural key already covers it. `users` and `blacklist` are reference
tables and key on their own primary key, same as any non-distributed table.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any

from cb_core import db
from cb_core.logging import get_logger
from cb_worker.importer import TableLoad

log = get_logger("cb.importer.loader")

#: How to write each target table. Order here has no effect on write order —
#: that is `runner.py`'s job, because it also has to interleave the
#: missing-`groups`-stub insert — but the tables are listed in roughly
#: FK-dependency order for a human reading top to bottom.
TABLE_LOADS: dict[str, TableLoad] = {
    "groups": TableLoad(
        table="groups",
        columns=("group_id", "title", "image_url"),
        conflict_columns=("group_id",),
        update_columns=("title", "image_url"),
    ),
    "group_configs": TableLoad(
        table="group_configs",
        columns=(
            "group_id",
            "allow_furbots",
            "sticker_spam_limit",
            "sticker_spam_window_s",
            "media_restrict_seconds",
            "captcha_timeout_seconds",
            "functions_fun",
            "functions_utility",
            "sfw",
            "language",
            "publisher_post",
            "publisher_ask",
            "publisher_members_only",
            "thread_posts",
            "max_posts",
            "doomlist_enabled",
            "updated_at",  # appended by load_rows, see _STAMP_COLUMN
        ),
        conflict_columns=("group_id",),
        update_columns=(
            "allow_furbots",
            "sticker_spam_limit",
            "media_restrict_seconds",
            "captcha_timeout_seconds",
            "functions_fun",
            "functions_utility",
            "sfw",
            "language",
            "publisher_post",
            "publisher_ask",
            "publisher_members_only",
            "thread_posts",
            "max_posts",
            "updated_at",
            # sticker_spam_window_s, doomlist_enabled: deliberately excluded,
            # see module docstring.
        ),
    ),
    "group_rules": TableLoad(
        table="group_rules",
        columns=("group_id", "body", "updated_at"),
        conflict_columns=("group_id",),
        update_columns=("body", "updated_at"),
    ),
    "group_welcomes": TableLoad(
        table="group_welcomes",
        columns=("group_id", "body", "updated_at"),
        conflict_columns=("group_id",),
        update_columns=("body", "updated_at"),
    ),
    "group_admins": TableLoad(
        table="group_admins",
        columns=("group_id", "user_id", "role", "anonymous", "synced_at"),
        conflict_columns=("group_id", "user_id"),
        update_columns=("role", "anonymous", "synced_at"),
    ),
    "users": TableLoad(
        table="users",
        columns=(
            "user_id",
            "username",
            "first_name",
            "last_name",
            "language_code",
            "birthdate",
            "updated_at",
        ),
        conflict_columns=("user_id",),
        update_columns=(
            "username",
            "first_name",
            "last_name",
            "language_code",
            "birthdate",
            "updated_at",
        ),
    ),
    "blacklist": TableLoad(
        table="blacklist",
        columns=("subject_id", "kind", "reason", "source"),
        conflict_columns=("subject_id",),
        update_columns=(),
    ),
    "sticker_pool": TableLoad(
        table="sticker_pool",
        columns=("file_id",),
        conflict_columns=("file_id",),
        update_columns=(),
    ),
}

#: Tables whose last INSERT column is a "when did the import last touch this"
#: timestamp with no per-row value from the mapper (see module docstring). The
#: value is generated once per `load_rows` call and appended to every row.
_STAMP_COLUMN: dict[str, str] = {
    "group_configs": "updated_at",
    "group_rules": "updated_at",
    "group_welcomes": "updated_at",
    "group_admins": "synced_at",
    "users": "updated_at",
}


def _upsert_sql(load: TableLoad) -> str:
    """`INSERT ... ON CONFLICT (<natural key>) DO UPDATE|NOTHING`, fully parameterised.

    Column and table names come from `TABLE_LOADS` only — never from a row's
    contents — so this is safe to build with an f-string even though the rest of
    the codebase bans string-built SQL: no caller-controlled value ever reaches
    the statement text, only `$n` placeholders do (AGENTS.md §4, rule 5).
    """
    placeholders = ", ".join(f"${i + 1}" for i in range(len(load.columns)))
    conflict = ", ".join(load.conflict_columns)
    if load.update_columns:
        assignments = ", ".join(f"{c} = EXCLUDED.{c}" for c in load.update_columns)
        action = f"DO UPDATE SET {assignments}"
    else:
        # Nothing to update means nothing on this table is v1-owned in a way
        # worth reasserting (blacklist) — a repeat insert is a no-op.
        action = "DO NOTHING"
    return (
        f"INSERT INTO {load.table} ({', '.join(load.columns)}) "
        f"VALUES ({placeholders}) "
        f"ON CONFLICT ({conflict}) {action}"
    )


async def load_rows(table: str, rows: Sequence[tuple[Any, ...]], *, batch_size: int) -> int:
    """Upsert `rows` into `table` in batches of `batch_size`, return the count written.

    `table` must be a key of `TABLE_LOADS` — this function does not accept an
    arbitrary table name or arbitrary SQL, only the shapes declared above, which
    is what keeps every write parameterised. For a table in `_STAMP_COLUMN`, one
    `now()` is generated here and appended to every row — once for the whole
    call, not once per row or per batch, so every row this call writes carries
    the same "last touched" instant.
    """
    if not rows:
        return 0
    try:
        load = TABLE_LOADS[table]
    except KeyError:
        raise ValueError(f"no TableLoad registered for table {table!r}") from None

    stamp_column = _STAMP_COLUMN.get(table)
    if stamp_column is not None:
        now = datetime.now(UTC)
        rows = [(*row, now) for row in rows]

    stmt = _upsert_sql(load)
    written = 0
    for start in range(0, len(rows), batch_size):
        batch = rows[start : start + batch_size]
        await db.executemany(stmt, batch, name=f"import_{table}")
        written += len(batch)
    log.info("import.table.written", table=table, rows=written)
    return written


async def ensure_group_stubs(group_ids: Iterable[int]) -> int:
    """Insert a bare `groups` row for any id in `group_ids` not already present.

    A `configs`/`rules`/`welcomes`/`groups`(-admins) document can reference a
    `group_id` that never had (or has not yet had, given collections are read in
    whatever order the mapper layer is asked for) its own `groups` document
    mapped — v1's Mongo has no referential integrity to guarantee otherwise. The
    alternative to a stub row is skipping the child row outright, which throws
    away real user data (an admin's `/newrules` text, a `/newwelcome` message)
    for what is usually a bookkeeping gap, not evidence the group is gone. So:
    create the minimal row (`ON CONFLICT DO NOTHING`, never overwriting a real
    `groups` row a later or earlier batch already wrote) and let the child insert
    proceed; if `groups` never turns up a document for that id, cb-worker's own
    membership tracking will fill in `title`/`chat_type` the next time the bot
    sees the group, exactly as it does for any group created organically on v2.
    """
    ids = sorted({int(g) for g in group_ids})
    if not ids:
        return 0
    result = await db.execute(
        """
        INSERT INTO groups (group_id)
        SELECT unnest($1::bigint[])
        ON CONFLICT (group_id) DO NOTHING
        """,
        ids,
        name="import_group_stub",
    )
    log.info("import.group_stub.ensured", candidates=len(ids), result=result)
    return len(ids)
