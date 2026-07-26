"""Test data factories — build groups and users the way the bot does.

Integration tests must not hand-write INSERTs: the point of this layer is that
the rows look like the ones production writes, including the distribution column
and the UUIDv7 keys. Everything here is scoped to one group id so tests are
isolated from each other.
"""

from __future__ import annotations

import itertools
import random
from collections.abc import Callable
from typing import Any

from cb_core import db

# Telegram supergroup ids are large negatives; a per-run base keeps concurrent
# test runs from colliding on a shared database.
_RUN_BASE = -1_00_000_000_000 - random.randrange(1, 9_000_000)
_group_seq = itertools.count(1)
_user_seq = itertools.count(1)


class SimulatedUser:
    __slots__ = ("first_name", "is_admin", "user_id", "username")

    def __init__(self, user_id: int, username: str, first_name: str, is_admin: bool) -> None:
        self.user_id = user_id
        self.username = username
        self.first_name = first_name
        self.is_admin = is_admin

    def __repr__(self) -> str:  # pragma: no cover
        return f"SimulatedUser({self.username}, admin={self.is_admin})"


class World:
    """One group, its config, and its members."""

    def __init__(self, run: Callable[[Any], Any]) -> None:
        self._run = run
        self.group_id: int = _RUN_BASE - next(_group_seq) * 1000
        self.users: list[SimulatedUser] = []

    # ---------------------------------------------------------------- lifecycle

    def setup(self) -> None:
        self._run(self._create_group())

    def teardown(self) -> None:
        # ON DELETE CASCADE clears configs, members, media and usage rows.
        self._run(
            db.execute(
                "DELETE FROM groups WHERE group_id = $1", self.group_id, name="test_teardown"
            )
        )
        if self.users:
            self._run(
                db.execute(
                    "DELETE FROM users WHERE user_id = ANY($1::bigint[])",
                    [u.user_id for u in self.users],
                    name="test_teardown_users",
                )
            )

    async def _create_group(self) -> None:
        await db.execute(
            """
            INSERT INTO groups (group_id, title, chat_type, skin)
            VALUES ($1, $2, 'supergroup', 'cookiebot')
            ON CONFLICT (group_id) DO NOTHING
            """,
            self.group_id,
            f"QA Group {self.group_id}",
            name="factory_group",
        )
        await db.execute(
            "INSERT INTO group_configs (group_id) VALUES ($1) ON CONFLICT DO NOTHING",
            self.group_id,
            name="factory_config",
        )

    # -------------------------------------------------------------------- users

    def add_user(self, *, admin: bool = False, username: str | None = None) -> SimulatedUser:
        n = next(_user_seq)
        user = SimulatedUser(
            user_id=500_000_000 + n,
            username=username or f"tester{n}",
            first_name=f"Tester {n}",
            is_admin=admin,
        )
        self._run(self._persist_user(user))
        self.users.append(user)
        return user

    def add_users(self, count: int, *, admins: int = 0) -> list[SimulatedUser]:
        return [self.add_user(admin=i < admins) for i in range(count)]

    async def _persist_user(self, user: SimulatedUser) -> None:
        await db.execute(
            """
            INSERT INTO users (user_id, username, first_name)
            VALUES ($1,$2,$3)
            ON CONFLICT (user_id) DO UPDATE SET username = EXCLUDED.username
            """,
            user.user_id,
            user.username,
            user.first_name,
            name="factory_user",
        )
        await db.execute(
            """
            INSERT INTO group_members (group_id, user_id)
            VALUES ($1,$2) ON CONFLICT DO NOTHING
            """,
            self.group_id,
            user.user_id,
            name="factory_member",
        )
        if user.is_admin:
            await db.execute(
                """
                INSERT INTO group_admins (group_id, user_id, role)
                VALUES ($1,$2,'administrator') ON CONFLICT DO NOTHING
                """,
                self.group_id,
                user.user_id,
                name="factory_admin",
            )

    # ------------------------------------------------------------------ helpers

    def set_config(self, **fields: object) -> None:
        if not fields:
            return
        # Column names come from the test's own keyword arguments and values are
        # bound parameters — no caller-controlled string reaches the query.
        assignments = ", ".join(f"{k} = ${i + 2}" for i, k in enumerate(fields))
        self._run(
            db.execute(
                f"UPDATE group_configs SET {assignments} WHERE group_id = $1",
                self.group_id,
                *fields.values(),
                name="factory_set_config",
            )
        )

    def count(self, table: str) -> int:
        allowed = {"media_objects", "llm_usage", "group_members", "group_admins"}
        if table not in allowed:
            raise ValueError(f"refusing to count {table!r}")
        row = self._run(
            db.fetchrow(
                f"SELECT count(*) AS n FROM {table} WHERE group_id = $1",
                self.group_id,
                name="factory_count",
            )
        )
        return int(row["n"])
