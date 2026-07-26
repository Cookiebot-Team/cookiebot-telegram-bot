"""util_doomlist's local check against a real database.

`cb_gateway.handlers.doomlist.check_local_blacklist` replaces v1's two HTTP
reads against the Java backend (`blacklist/{id}`, `blacklist/username/{username}`,
both through `get_request_backend`'s `verify=False, timeout=60` —
FEATURE-MAP D2, `../COOKIEBOT-Telegram-Group-Bot/Bot/universal_funcs.py:96-104`)
with one query against the reference `blacklist` table (migration 0001),
replicated to every node. This exercises the real round trip, including the
username lookup adapted to the v2 schema (`blacklist` has no username column,
so it joins through `users.username` — see docs/contracts/util_doomlist.md's
"v2 architecture" section for why).

No monkeypatching of our own code: `check_local_blacklist` is exercised as-is
against real rows the `world` factory seeds (AGENTS.md §6). Test bodies stay
synchronous and drive the coroutine under test through the session-scoped
`run` fixture, same convention as qa/integration/test_group_config.py.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Iterator
from types import ModuleType
from typing import Any

import pytest

from cb_core import db
from cb_gateway.handlers import doomlist
from qa.integration.factories import World

pytestmark = pytest.mark.integration

Run = Callable[[Coroutine[Any, Any, Any]], Any]


class _JoinerLike:
    """Only the attributes `check_local_blacklist` reads — a real aiogram
    `User` would work too, but the handler's own contract is "any object with
    `.id`, `.username`, `.full_name`", so a minimal stand-in proves that."""

    def __init__(self, user_id: int, username: str | None, full_name: str = "Newcomer") -> None:
        self.id = user_id
        self.username = username
        self.full_name = full_name


@pytest.fixture
def _clean_blacklist(run: Run) -> Iterator[Callable[[int], int]]:
    """`blacklist` is a reference table with no `group_id` column, so the
    per-group `_table_cleaner` idiom from qa/conftest.py doesn't apply — this
    deletes only the rows this file's own tests insert.
    """
    ids: list[int] = []

    def _track(user_id: int) -> int:
        ids.append(user_id)
        return user_id

    yield _track
    if ids:
        run(
            db.execute(
                "DELETE FROM blacklist WHERE subject_id = ANY($1::bigint[])",
                ids,
                name="test_doomlist_cleanup",
            )
        )


class TestBlacklistById:
    def test_blacklisted_subject_id_is_a_hit(
        self, run: Run, world: World, _clean_blacklist: Callable[[int], int]
    ) -> None:
        user = world.add_user()
        _clean_blacklist(user.user_id)
        run(
            db.execute(
                "INSERT INTO blacklist (subject_id, kind, source) VALUES ($1, 'user', 'manual')",
                user.user_id,
                name="test_seed_blacklist",
            )
        )

        joiner = _JoinerLike(user.user_id, user.username)
        assert run(doomlist.check_local_blacklist(joiner)) is True

    def test_unlisted_subject_id_is_not_a_hit(self, run: Run, world: World) -> None:
        user = world.add_user()
        joiner = _JoinerLike(user.user_id, user.username)
        assert run(doomlist.check_local_blacklist(joiner)) is False


class TestBlacklistByUsername:
    def test_blacklisted_username_is_a_hit_via_the_users_join(
        self, run: Run, world: World, _clean_blacklist: Callable[[int], int]
    ) -> None:
        """v1's `blacklist/username/{username}` has no direct v2 column
        equivalent (`blacklist.subject_id` is bigint-only) — the joiner's own
        `user_id`, once recorded in `users` (as any real Telegram account would
        be, from a prior message), lets the join resolve the username to the
        blacklisted id.
        """
        user = world.add_user(username="known_raider")
        _clean_blacklist(user.user_id)
        run(
            db.execute(
                "INSERT INTO blacklist (subject_id, kind, source) VALUES ($1, 'user', 'manual')",
                user.user_id,
                name="test_seed_blacklist_username",
            )
        )

        # A different numeric id than the blacklisted row's, matching v1's own
        # two independent sub-checks (by id, by username) — only the username
        # should make this a hit.
        joiner = _JoinerLike(user.user_id + 1, "known_raider")
        assert run(doomlist.check_local_blacklist(joiner)) is True

    def test_username_match_is_case_insensitive(
        self, run: Run, world: World, _clean_blacklist: Callable[[int], int]
    ) -> None:
        user = world.add_user(username="MixedCase")
        _clean_blacklist(user.user_id)
        run(
            db.execute(
                "INSERT INTO blacklist (subject_id, kind, source) VALUES ($1, 'user', 'manual')",
                user.user_id,
                name="test_seed_blacklist_case",
            )
        )

        joiner = _JoinerLike(user.user_id + 1, "mixedcase")
        assert run(doomlist.check_local_blacklist(joiner)) is True

    def test_no_username_never_matches(self, run: Run, world: World) -> None:
        """v1 guards this branch with `if 'username' in msg['new_chat_participant']`
        (`GroupShield.py:208`) — a joiner with no username skips the username
        sub-check entirely rather than matching a NULL comparison."""
        joiner = _JoinerLike(999_999_999, None)
        assert run(doomlist.check_local_blacklist(joiner)) is False


class TestCitusTopology:
    """The local check must stay single-shard — AGENTS.md §4 rule 6. `blacklist`
    and `users` are both reference tables (replicated to every node), so the
    join is node-local regardless of `group_id` — there is none in this query.
    """

    @pytest.fixture(scope="module")
    def citus(self, pg: ModuleType, run: Run) -> ModuleType:
        row = run(pg.fetchrow("SELECT count(*) AS n FROM pg_extension WHERE extname = 'citus'"))
        if not row or row["n"] == 0:
            pytest.skip("citus extension not installed")
        return pg

    def test_blacklist_lookup_is_single_shard(self, citus: ModuleType, run: Run) -> None:
        plan = run(
            citus.fetch(
                """
                EXPLAIN (COSTS OFF)
                SELECT EXISTS (
                    SELECT 1 FROM blacklist WHERE kind = 'user' AND subject_id = $1
                ) OR EXISTS (
                    SELECT 1 FROM blacklist b
                    JOIN users u ON u.user_id = b.subject_id
                    WHERE b.kind = 'user' AND $2::text IS NOT NULL AND lower(u.username) = lower($2)
                ) AS hit
                """,
                123456,
                "someone",
            )
        )
        task_count = None
        for row in plan:
            line = str(row[0]).strip()
            if line.startswith("Task Count:"):
                task_count = int(line.split(":")[1])
                break
        # Reference tables (`blacklist`, `users`) are replicated to every node,
        # so a query touching only them either runs as a single-task Citus plan
        # or as a plain local plan with no "Task Count" line at all — either is
        # fine; what AGENTS.md §4 rule 6 rules out is a fan-out across many
        # tasks, which would only happen if this query ever joined a
        # `group_id`-distributed table without filtering on it.
        assert task_count is None or task_count == 1, "\n".join(str(r[0]) for r in plan)
