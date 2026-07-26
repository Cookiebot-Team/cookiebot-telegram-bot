"""The sandbox's durable copy: a DuckDB file mirroring `SandboxStore`.

`state.py` is the read path — every lookup the control plane and the Telegram
surface make goes through its in-memory dicts, never through here. This
module exists only so a scenario survives a process restart, and so a second
process (the web UI server, a test run) can open the same file read-only and
see what happened without contending with the sandbox's own writer.

DuckDB's own concurrency model is exactly the one-writer-many-readers shape
this needs: a file opened `read_only=False` takes the write lock, and any
number of other processes may open the same file `read_only=True` at the
same time. There is deliberately no query surface here beyond `load_into` —
that method exists to repopulate `SandboxStore` at startup, not to be a
general-purpose reader; a second process that wants to inspect a run opens
its own `SandboxDB(path, read_only=True)` and reads the tables directly.

Every write method degrades to a logged warning on failure rather than
raising: a workbench that refuses to run because its notebook is locked is
worse than one that forgets a row. `state.py` relies on that guarantee to
call these methods unconditionally on every mutation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import duckdb
from cb_sandbox.logging import get_logger

if TYPE_CHECKING:
    from cb_sandbox.files import SandboxFile
    from cb_sandbox.state import (
        Membership,
        SandboxChat,
        SandboxMessage,
        SandboxScenario,
        SandboxStore,
        SandboxUser,
    )

log = get_logger("cb.sandbox.persistence")

#: Individual DDL statements, not one multi-statement string: DuckDB's Python
#: `execute()` runs exactly one statement per call.
_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS sandbox_users (
        user_id BIGINT PRIMARY KEY,
        first_name VARCHAR NOT NULL,
        username VARCHAR NOT NULL,
        last_name VARCHAR,
        language_code VARCHAR NOT NULL,
        is_bot BOOLEAN NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sandbox_chats (
        chat_id BIGINT PRIMARY KEY,
        title VARCHAR NOT NULL,
        type VARCHAR NOT NULL,
        username VARCHAR,
        description VARCHAR,
        pinned_message_id BIGINT,
        default_permissions VARCHAR
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sandbox_members (
        chat_id BIGINT NOT NULL,
        user_id BIGINT NOT NULL,
        role VARCHAR NOT NULL,
        anonymous BOOLEAN NOT NULL,
        joined_at DOUBLE NOT NULL,
        restricted_until DOUBLE NOT NULL,
        permissions VARCHAR,
        PRIMARY KEY (chat_id, user_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sandbox_messages (
        chat_id BIGINT NOT NULL,
        message_id BIGINT NOT NULL,
        from_id BIGINT NOT NULL,
        text VARCHAR,
        date DOUBLE NOT NULL,
        sender_chat_id BIGINT,
        reply_to_message_id BIGINT,
        entities VARCHAR NOT NULL,
        reply_markup VARCHAR,
        media VARCHAR,
        media_caption VARCHAR,
        edited BOOLEAN NOT NULL,
        deleted BOOLEAN NOT NULL,
        caption_entities VARCHAR,
        link_preview_options VARCHAR,
        message_thread_id BIGINT,
        forward_origin VARCHAR,
        media_extra VARCHAR,
        service VARCHAR,
        PRIMARY KEY (chat_id, message_id)
    )
    """,
    "CREATE SEQUENCE IF NOT EXISTS sandbox_api_call_id START 1",
    """
    CREATE TABLE IF NOT EXISTS sandbox_api_calls (
        id BIGINT PRIMARY KEY DEFAULT nextval('sandbox_api_call_id'),
        method VARCHAR NOT NULL,
        payload VARCHAR NOT NULL,
        called_at DOUBLE NOT NULL
    )
    """,
    # The one table `SandboxDB.clear()` (called by `SandboxStore.reset()`)
    # deliberately never touches — see `SandboxStore.next_update_id`'s
    # docstring for why an update/message id counter must outlive the world
    # it counts.
    """
    CREATE TABLE IF NOT EXISTS sandbox_counters (
        name VARCHAR PRIMARY KEY,
        value BIGINT NOT NULL
    )
    """,
    # A `SandboxScenario`'s id is caller-chosen (a test's nodeid, typically),
    # not a synthetic sequence, so it is the primary key outright rather than
    # a separate surrogate + unique-constraint pair.
    """
    CREATE TABLE IF NOT EXISTS sandbox_scenarios (
        id VARCHAR PRIMARY KEY,
        name VARCHAR NOT NULL,
        description VARCHAR,
        source VARCHAR,
        tags VARCHAR NOT NULL,
        metadata VARCHAR NOT NULL,
        status VARCHAR NOT NULL,
        notes VARCHAR NOT NULL,
        started_at DOUBLE NOT NULL,
        ended_at DOUBLE
    )
    """,
    # `ALTER ... ADD COLUMN IF NOT EXISTS` rather than relying on the `CREATE
    # TABLE IF NOT EXISTS` above: a `sandbox.duckdb` file created by an older
    # build of this file already has these tables, just without the new
    # columns, and `CREATE TABLE IF NOT EXISTS` is a no-op against an
    # existing table — it does not retroactively add columns.
    "ALTER TABLE sandbox_chats ADD COLUMN IF NOT EXISTS username VARCHAR",
    "ALTER TABLE sandbox_chats ADD COLUMN IF NOT EXISTS description VARCHAR",
    "ALTER TABLE sandbox_chats ADD COLUMN IF NOT EXISTS pinned_message_id BIGINT",
    "ALTER TABLE sandbox_chats ADD COLUMN IF NOT EXISTS default_permissions VARCHAR",
    "ALTER TABLE sandbox_members ADD COLUMN IF NOT EXISTS permissions VARCHAR",
    "ALTER TABLE sandbox_messages ADD COLUMN IF NOT EXISTS caption_entities VARCHAR",
    "ALTER TABLE sandbox_messages ADD COLUMN IF NOT EXISTS link_preview_options VARCHAR",
    "ALTER TABLE sandbox_messages ADD COLUMN IF NOT EXISTS message_thread_id BIGINT",
    "ALTER TABLE sandbox_messages ADD COLUMN IF NOT EXISTS forward_origin VARCHAR",
    "ALTER TABLE sandbox_messages ADD COLUMN IF NOT EXISTS media_extra VARCHAR",
    "ALTER TABLE sandbox_messages ADD COLUMN IF NOT EXISTS service VARCHAR",
    # The scenario tag: added to both tables a scenario stamps, same
    # additive-migration convention as everything above so an older
    # `sandbox.duckdb` still opens.
    "ALTER TABLE sandbox_messages ADD COLUMN IF NOT EXISTS scenario_id VARCHAR",
    "ALTER TABLE sandbox_api_calls ADD COLUMN IF NOT EXISTS scenario_id VARCHAR",
    # The bytes behind every photo, sticker and document in a run. Content
    # addressed (`files.py`), so re-attaching the same picture is one row.
    # BLOB rather than base64: DuckDB stores binary natively, and encoding it
    # would inflate every image by a third for no benefit to any reader.
    """
    CREATE TABLE IF NOT EXISTS sandbox_files (
        file_id VARCHAR PRIMARY KEY,
        file_unique_id VARCHAR NOT NULL,
        data BLOB NOT NULL,
        mime_type VARCHAR NOT NULL,
        file_name VARCHAR NOT NULL,
        width BIGINT NOT NULL,
        height BIGINT NOT NULL,
        duration BIGINT NOT NULL
    )
    """,
    "ALTER TABLE sandbox_messages ADD COLUMN IF NOT EXISTS media_file_id VARCHAR",
    # Which feature a scenario was exercising. Additive like everything above,
    # so a file written before feature grouping existed still opens — its
    # scenarios simply read back with `feature = NULL` and fall through to the
    # tag-based inference `control_api` does anyway.
    "ALTER TABLE sandbox_scenarios ADD COLUMN IF NOT EXISTS feature VARCHAR",
)


class SandboxDB:
    """A DuckDB file holding one sandbox run.

    Opened read-write (the sandbox process itself) or read-only (anyone else
    who wants to look). A read-only open of a file that does not exist yet —
    the normal state before the sandbox has run once — degrades to "no
    connection" rather than raising, so a reader started before a writer
    still sees an empty world instead of a crash.
    """

    def __init__(self, path: str, *, read_only: bool = False) -> None:
        self.path = path
        self.read_only = read_only
        self._conn: duckdb.DuckDBPyConnection | None = self._connect()
        if self._conn is not None and not read_only:
            self.create_schema()

    def _connect(self) -> duckdb.DuckDBPyConnection | None:
        if self.read_only and not Path(self.path).exists():
            return None
        try:
            return duckdb.connect(self.path, read_only=self.read_only)
        except Exception as exc:  # noqa: BLE001 - a broken database file must degrade, not crash
            log.warning(
                "sandbox.db.connect_failed",
                error=str(exc),
                path=self.path,
                read_only=self.read_only,
            )
            return None

    # ------------------------------------------------------------- schema

    def create_schema(self) -> None:
        if self._conn is None or self.read_only:
            return
        try:
            for statement in _SCHEMA_STATEMENTS:
                self._conn.execute(statement)
            # Fold the schema straight into the database file instead of
            # leaving it in the write-ahead log.
            #
            # This is not an optimisation. A sandbox process is normally ended
            # by SIGTERM (the e2e suite's own teardown, Ctrl-C in a terminal),
            # which does not close the connection, so the WAL survives — and
            # DuckDB cannot replay a WAL containing the `ALTER TABLE ... ADD
            # COLUMN` statements above: reopening the file fails with an
            # internal "Failure while replaying WAL file" error and the whole
            # recording is unreadable. Checkpointing here means the only thing
            # ever left in a WAL is row data, which replays fine.
            self._conn.execute("CHECKPOINT")
        except Exception as exc:  # noqa: BLE001 - schema setup must never crash the sandbox
            log.warning("sandbox.db.schema_failed", error=str(exc))

    # -------------------------------------------------------------- writes

    def _execute(self, op: str, statement: str, params: list[Any]) -> None:
        if self._conn is None or self.read_only:
            return
        try:
            self._conn.execute(statement, params)
        except Exception as exc:  # noqa: BLE001 - a write failure must warn, not raise
            log.warning("sandbox.db.write_failed", op=op, error=str(exc))

    def save_user(self, user: SandboxUser) -> None:
        self._execute(
            "save_user",
            """
            INSERT INTO sandbox_users
                (user_id, first_name, username, last_name, language_code, is_bot)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (user_id) DO UPDATE SET
                first_name = excluded.first_name,
                username = excluded.username,
                last_name = excluded.last_name,
                language_code = excluded.language_code,
                is_bot = excluded.is_bot
            """,
            [
                user.id,
                user.first_name,
                user.username,
                user.last_name,
                user.language_code,
                user.is_bot,
            ],
        )

    def save_bot(self, bot: SandboxUser) -> None:
        """The sandbox's bot identity is a `SandboxUser` with `is_bot=True` —
        there is no separate bots table. Named distinctly so a caller can say
        what it means without reaching into `sandbox_users` by hand."""
        self.save_user(bot)

    def save_chat(self, chat: SandboxChat) -> None:
        self._execute(
            "save_chat",
            """
            INSERT INTO sandbox_chats
                (chat_id, title, type, username, description, pinned_message_id,
                 default_permissions)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (chat_id) DO UPDATE SET
                title = excluded.title,
                type = excluded.type,
                username = excluded.username,
                description = excluded.description,
                pinned_message_id = excluded.pinned_message_id,
                default_permissions = excluded.default_permissions
            """,
            [
                chat.id,
                chat.title,
                chat.type,
                chat.username,
                chat.description,
                chat.pinned_message_id,
                json.dumps(chat.default_permissions) if chat.default_permissions else None,
            ],
        )

    def save_member(self, chat_id: int, membership: Membership) -> None:
        self._execute(
            "save_member",
            """
            INSERT INTO sandbox_members
                (chat_id, user_id, role, anonymous, joined_at, restricted_until, permissions)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (chat_id, user_id) DO UPDATE SET
                role = excluded.role,
                anonymous = excluded.anonymous,
                joined_at = excluded.joined_at,
                restricted_until = excluded.restricted_until,
                permissions = excluded.permissions
            """,
            [
                chat_id,
                membership.user_id,
                membership.role,
                membership.anonymous,
                membership.joined_at,
                membership.restricted_until,
                json.dumps(membership.permissions) if membership.permissions else None,
            ],
        )

    def save_message(self, message: SandboxMessage) -> None:
        self._execute(
            "save_message",
            """
            INSERT INTO sandbox_messages
                (chat_id, message_id, from_id, text, date, sender_chat_id,
                 reply_to_message_id, entities, reply_markup, media, media_caption,
                 edited, deleted, caption_entities, link_preview_options,
                 message_thread_id, forward_origin, media_extra, service, scenario_id,
                 media_file_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (chat_id, message_id) DO UPDATE SET
                from_id = excluded.from_id,
                text = excluded.text,
                date = excluded.date,
                sender_chat_id = excluded.sender_chat_id,
                reply_to_message_id = excluded.reply_to_message_id,
                entities = excluded.entities,
                reply_markup = excluded.reply_markup,
                media = excluded.media,
                media_caption = excluded.media_caption,
                edited = excluded.edited,
                deleted = excluded.deleted,
                caption_entities = excluded.caption_entities,
                link_preview_options = excluded.link_preview_options,
                message_thread_id = excluded.message_thread_id,
                forward_origin = excluded.forward_origin,
                media_extra = excluded.media_extra,
                service = excluded.service,
                scenario_id = excluded.scenario_id,
                media_file_id = excluded.media_file_id
            """,
            [
                message.chat_id,
                message.message_id,
                message.from_id,
                message.text,
                message.date,
                message.sender_chat_id,
                message.reply_to_message_id,
                json.dumps(message.entities),
                json.dumps(message.reply_markup) if message.reply_markup is not None else None,
                message.media,
                message.media_caption,
                message.edited,
                message.deleted,
                json.dumps(message.caption_entities) if message.caption_entities else None,
                json.dumps(message.link_preview_options)
                if message.link_preview_options is not None
                else None,
                message.message_thread_id,
                json.dumps(message.forward_origin) if message.forward_origin is not None else None,
                json.dumps(message.media_extra) if message.media_extra else None,
                json.dumps(message.service) if message.service is not None else None,
                message.scenario_id,
                message.media_file_id,
            ],
        )

    def save_file(self, stored: SandboxFile) -> None:
        """Content-addressed, so this is an insert-or-ignore rather than an
        upsert: the same `file_id` always means the same bytes, and rewriting
        a multi-megabyte BLOB to replace it with itself is pure cost."""
        self._execute(
            "save_file",
            """
            INSERT INTO sandbox_files
                (file_id, file_unique_id, data, mime_type, file_name, width, height, duration)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (file_id) DO NOTHING
            """,
            [
                stored.file_id,
                stored.file_unique_id,
                stored.data,
                stored.mime_type,
                stored.file_name,
                stored.width,
                stored.height,
                stored.duration,
            ],
        )

    def save_api_call(
        self, method: str, payload: dict[str, Any], at: float, scenario_id: str | None = None
    ) -> None:
        # No natural key: each call is recorded once and never re-saved, so
        # there is nothing to make idempotent (unlike the rows above, which
        # `state.py` re-saves on every follow-up mutation).
        self._execute(
            "save_api_call",
            "INSERT INTO sandbox_api_calls (method, payload, called_at, scenario_id) "
            "VALUES (?, ?, ?, ?)",
            [method, json.dumps(payload), at, scenario_id],
        )

    def save_scenario(self, scenario: SandboxScenario) -> None:
        self._execute(
            "save_scenario",
            """
            INSERT INTO sandbox_scenarios
                (id, name, description, source, feature, tags, metadata, status, notes,
                 started_at, ended_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET
                name = excluded.name,
                description = excluded.description,
                source = excluded.source,
                feature = excluded.feature,
                tags = excluded.tags,
                metadata = excluded.metadata,
                status = excluded.status,
                notes = excluded.notes,
                started_at = excluded.started_at,
                ended_at = excluded.ended_at
            """,
            [
                scenario.id,
                scenario.name,
                scenario.description,
                scenario.source,
                scenario.feature,
                json.dumps(scenario.tags),
                json.dumps(scenario.metadata),
                scenario.status,
                json.dumps(scenario.notes),
                scenario.started_at,
                scenario.ended_at,
            ],
        )

    def save_counter(self, name: str, value: int) -> None:
        """The durable high-water mark behind `SandboxStore.next_update_id`/
        `next_message_id` — see that docstring for why these two counters,
        alone among everything in this file, must survive `clear()`."""
        self._execute(
            "save_counter",
            """
            INSERT INTO sandbox_counters (name, value)
            VALUES (?, ?)
            ON CONFLICT (name) DO UPDATE SET value = excluded.value
            """,
            [name, value],
        )

    def load_counters(self) -> dict[str, int]:
        if self._conn is None:
            return {}
        try:
            rows = self._conn.execute("SELECT name, value FROM sandbox_counters").fetchall()
        except Exception as exc:  # noqa: BLE001 - a corrupt file must degrade to empty, not crash
            log.warning("sandbox.db.load_counters_failed", error=str(exc))
            return {}
        return dict(rows)

    # --------------------------------------------------------------- reads

    def load_into(self, store: SandboxStore) -> None:
        """Repopulate `store`'s in-memory dicts from disk. A missing or
        unreadable database leaves `store` exactly as it was — empty, for a
        store that has just been constructed."""
        if self._conn is None:
            return
        from cb_sandbox.files import SandboxFile
        from cb_sandbox.state import (
            ChatType,
            Membership,
            Role,
            SandboxChat,
            SandboxMessage,
            SandboxScenario,
            SandboxUser,
        )

        try:
            user_rows = self._conn.execute(
                "SELECT user_id, first_name, username, last_name, language_code, is_bot "
                "FROM sandbox_users"
            ).fetchall()
            chat_rows = self._conn.execute(
                "SELECT chat_id, title, type, username, description, pinned_message_id, "
                "default_permissions FROM sandbox_chats"
            ).fetchall()
            member_rows = self._conn.execute(
                "SELECT chat_id, user_id, role, anonymous, joined_at, restricted_until, "
                "permissions FROM sandbox_members"
            ).fetchall()
            message_rows = self._conn.execute(
                "SELECT chat_id, message_id, from_id, text, date, sender_chat_id, "
                "reply_to_message_id, entities, reply_markup, media, media_caption, "
                "edited, deleted, caption_entities, link_preview_options, "
                "message_thread_id, forward_origin, media_extra, service, scenario_id, "
                "media_file_id FROM sandbox_messages ORDER BY chat_id, message_id"
            ).fetchall()
            api_call_rows = self._conn.execute(
                "SELECT method, payload, called_at, scenario_id FROM sandbox_api_calls ORDER BY id"
            ).fetchall()
            file_rows = self._conn.execute(
                "SELECT file_id, file_unique_id, data, mime_type, file_name, width, "
                "height, duration FROM sandbox_files"
            ).fetchall()
            scenario_rows = self._conn.execute(
                "SELECT id, name, description, source, feature, tags, metadata, status, "
                "notes, started_at, ended_at FROM sandbox_scenarios ORDER BY started_at"
            ).fetchall()
        except Exception as exc:  # noqa: BLE001 - a corrupt file must degrade to empty, not crash
            log.warning("sandbox.db.load_failed", error=str(exc))
            return

        for user_id, first_name, username, last_name, language_code, is_bot in user_rows:
            store.users[user_id] = SandboxUser(
                id=user_id,
                first_name=first_name,
                username=username,
                last_name=last_name,
                language_code=language_code,
                is_bot=is_bot,
            )
        for (
            chat_id,
            title,
            chat_type,
            username,
            description,
            pinned_message_id,
            permissions,
        ) in chat_rows:
            store.chats[chat_id] = SandboxChat(
                id=chat_id,
                title=title,
                type=cast(ChatType, chat_type),
                username=username,
                description=description,
                pinned_message_id=pinned_message_id,
                default_permissions=json.loads(permissions) if permissions else {},
            )
        for (
            chat_id,
            user_id,
            role,
            anonymous,
            joined_at,
            restricted_until,
            permissions,
        ) in member_rows:
            chat = store.chats.get(chat_id)
            if chat is None:
                continue  # orphaned row from a hand-edited file; nothing to attach it to
            chat.members[user_id] = Membership(
                user_id=user_id,
                role=cast(Role, role),
                anonymous=anonymous,
                joined_at=joined_at,
                restricted_until=restricted_until,
                permissions=json.loads(permissions) if permissions else {},
            )
        for (
            chat_id,
            message_id,
            from_id,
            text,
            date,
            sender_chat_id,
            reply_to_message_id,
            entities,
            reply_markup,
            media,
            media_caption,
            edited,
            deleted,
            caption_entities,
            link_preview_options,
            message_thread_id,
            forward_origin,
            media_extra,
            service,
            scenario_id,
            media_file_id,
        ) in message_rows:
            message = SandboxMessage(
                message_id=message_id,
                chat_id=chat_id,
                from_id=from_id,
                text=text,
                date=date,
                sender_chat_id=sender_chat_id,
                reply_to_message_id=reply_to_message_id,
                entities=json.loads(entities) if entities else [],
                reply_markup=json.loads(reply_markup) if reply_markup else None,
                media=media,
                media_caption=media_caption,
                edited=edited,
                deleted=deleted,
                caption_entities=json.loads(caption_entities) if caption_entities else [],
                link_preview_options=json.loads(link_preview_options)
                if link_preview_options
                else None,
                message_thread_id=message_thread_id,
                forward_origin=json.loads(forward_origin) if forward_origin else None,
                media_extra=json.loads(media_extra) if media_extra else {},
                service=json.loads(service) if service else None,
                scenario_id=scenario_id,
                media_file_id=media_file_id,
            )
            store.messages.setdefault(chat_id, []).append(message)
        for (
            file_id,
            file_unique_id,
            data,
            mime_type,
            file_name,
            width,
            height,
            duration,
        ) in file_rows:
            # `restore`, not `add`: the id on disk is the authority. Re-deriving
            # it would be the same hash in practice, but a file written by an
            # older build with a different scheme would silently change id and
            # orphan every message pointing at it.
            store.files.restore(
                SandboxFile(
                    file_id=file_id,
                    file_unique_id=file_unique_id,
                    data=bytes(data),
                    mime_type=mime_type,
                    file_name=file_name,
                    width=width,
                    height=height,
                    duration=duration,
                )
            )
        for method, payload, at, scenario_id in api_call_rows:
            store.api_calls.append(
                {
                    "method": method,
                    "payload": json.loads(payload),
                    "at": at,
                    "scenario_id": scenario_id,
                }
            )
        for (
            scenario_id,
            name,
            description,
            source,
            feature,
            tags,
            metadata,
            status,
            notes,
            started_at,
            ended_at,
        ) in scenario_rows:
            store.scenarios[scenario_id] = SandboxScenario(
                id=scenario_id,
                name=name,
                description=description,
                source=source,
                feature=feature,
                tags=json.loads(tags) if tags else [],
                metadata=json.loads(metadata) if metadata else {},
                status=status,
                notes=json.loads(notes) if notes else [],
                started_at=started_at,
                ended_at=ended_at,
            )

    # -------------------------------------------------------------- reset

    def clear(self) -> None:
        """Wipe the world — but not `sandbox_counters`. Update and message ids
        are the one thing that must outlive a reset (see
        `SandboxStore.next_update_id`): a bot's own update dedupe, and
        anything its database remembers about a (chat_id, message_id) pair,
        have no idea the sandbox was reset — so rewinding either counter
        here would make the next batch of ids collide with ones already
        spent.
        """
        if self._conn is None or self.read_only:
            return
        try:
            # Fixed statements, not built from a variable: children before
            # parents keeps this correct even if foreign keys are added later.
            self._conn.execute("DELETE FROM sandbox_messages")
            self._conn.execute("DELETE FROM sandbox_members")
            self._conn.execute("DELETE FROM sandbox_chats")
            self._conn.execute("DELETE FROM sandbox_users")
            self._conn.execute("DELETE FROM sandbox_api_calls")
            self._conn.execute("DELETE FROM sandbox_scenarios")
            self._conn.execute("DELETE FROM sandbox_files")
        except Exception as exc:  # noqa: BLE001 - clearing must never crash the sandbox
            log.warning("sandbox.db.clear_failed", error=str(exc))

    def checkpoint(self) -> None:
        """Fold the write-ahead log into the database file.

        Called at shutdown (`cb_sandbox.app`'s lifespan) so a recording left
        behind by a terminated sandbox is a single self-contained file rather
        than a file plus a WAL that the next reader has to replay. See
        `create_schema` for why an unreplayable WAL is not a theoretical
        concern here.
        """
        if self._conn is None or self.read_only:
            return
        try:
            self._conn.execute("CHECKPOINT")
        except Exception as exc:  # noqa: BLE001 - a checkpoint failure must not crash shutdown
            log.warning("sandbox.db.checkpoint_failed", error=str(exc))

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception as exc:  # noqa: BLE001 - closing must never crash the sandbox
                log.warning("sandbox.db.close_failed", error=str(exc))
            self._conn = None
