"""`captcha_challenges` against a real Citus database.

Exercises the DB seam `cb_gateway.handlers.groupguardian` owns (`_issue_row`,
`_fetch_pending`, `_fetch_by_message`, `_record_wrong_attempt`,
`_delete_challenge`) against the real `captcha_challenges` table
(`packages/cb-api/migrations/versions/0001_initial_schema.py`), plus the Citus
single-shard guarantee those reads/writes depend on: `captcha_challenges` is
distributed on `group_id`, colocated with `groups` (AGENTS.md §4). Mirrors the
pattern in `qa/integration/test_group_welcomes.py`.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import TYPE_CHECKING, Any

import pytest

from cb_core import captcha
from cb_gateway.handlers import groupguardian as gg

if TYPE_CHECKING:
    from qa.integration.factories import World

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def citus(pg: ModuleType, run: Callable[[Coroutine[Any, Any, Any]], Any]) -> ModuleType:
    """Same guard as qa/integration/test_citus_topology.py: skip on plain Postgres."""
    row = run(pg.fetchrow("SELECT count(*) AS n FROM pg_extension WHERE extname = 'citus'"))
    if not row or row["n"] == 0:
        pytest.skip("citus extension not installed")
    return pg


def _expires_in(seconds: int) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=seconds)


class TestIssueAndFetch:
    def test_a_group_with_no_row_has_no_pending_challenge(
        self, run: Callable[[Coroutine[Any, Any, Any]], Any], world: World
    ) -> None:
        assert run(gg._fetch_pending(world.group_id, 999)) is None  # noqa: SLF001

    def test_issuing_a_challenge_makes_it_fetchable(
        self, run: Callable[[Coroutine[Any, Any, Any]], Any], world: World
    ) -> None:
        user = world.add_user()
        challenge = captcha.make_arithmetic()

        run(gg._issue_row(world.group_id, user.user_id, challenge, 42, _expires_in(300)))  # noqa: SLF001

        row = run(gg._fetch_pending(world.group_id, user.user_id))  # noqa: SLF001
        assert row is not None
        assert row["nonce"] == challenge.nonce
        assert row["kind"] == challenge.kind
        assert row["answer"] == challenge.answer
        assert row["attempts"] == 0
        assert row["message_id"] == 42

    def test_rejoin_rearms_the_challenge_instead_of_conflicting(
        self, run: Callable[[Coroutine[Any, Any, Any]], Any], world: World
    ) -> None:
        """The PK is `(group_id, user_id)` — a user who leaves mid-challenge
        and rejoins must get a fresh nonce/answer/attempts, not an insert
        failure or a stale row (v1 had no such collision at all: a flat file
        keyed by nothing, GroupShield.py:250-265)."""
        user = world.add_user()
        first = captcha.make_arithmetic()
        run(gg._issue_row(world.group_id, user.user_id, first, 1, _expires_in(300)))  # noqa: SLF001
        run(gg._record_wrong_attempt(world.group_id, user.user_id, 3))  # noqa: SLF001

        second = captcha.make_arithmetic()
        run(gg._issue_row(world.group_id, user.user_id, second, 2, _expires_in(300)))  # noqa: SLF001

        row = run(gg._fetch_pending(world.group_id, user.user_id))  # noqa: SLF001
        assert row is not None
        assert row["nonce"] == second.nonce
        assert row["answer"] == second.answer
        assert row["attempts"] == 0
        assert row["message_id"] == 2

    def test_solved_rows_are_not_returned_as_pending(
        self, run: Callable[[Coroutine[Any, Any, Any]], Any], world: World, pg: ModuleType
    ) -> None:
        """Defensive: `_fetch_pending`/`_fetch_by_message` both filter
        `solved_at IS NULL` even though this handler currently always
        deletes on resolution rather than setting `solved_at` — belt and
        braces against a future write path that marks instead of deletes."""
        user = world.add_user()
        challenge = captcha.make_arithmetic()
        run(gg._issue_row(world.group_id, user.user_id, challenge, 7, _expires_in(300)))  # noqa: SLF001
        run(
            pg.execute(
                "UPDATE captcha_challenges SET solved_at = now() "
                "WHERE group_id = $1 AND user_id = $2",
                world.group_id,
                user.user_id,
                name="test_mark_solved",
            )
        )

        assert run(gg._fetch_pending(world.group_id, user.user_id)) is None  # noqa: SLF001
        assert run(gg._fetch_by_message(world.group_id, 7)) is None  # noqa: SLF001


class TestFetchByMessage:
    def test_matches_the_message_the_challenge_was_sent_as(
        self, run: Callable[[Coroutine[Any, Any, Any]], Any], world: World
    ) -> None:
        user = world.add_user()
        challenge = captcha.make_arithmetic()
        run(gg._issue_row(world.group_id, user.user_id, challenge, 555, _expires_in(300)))  # noqa: SLF001

        row = run(gg._fetch_by_message(world.group_id, 555))  # noqa: SLF001
        assert row is not None
        assert row["user_id"] == user.user_id
        assert row["nonce"] == challenge.nonce

    def test_a_different_message_id_does_not_match(
        self, run: Callable[[Coroutine[Any, Any, Any]], Any], world: World
    ) -> None:
        user = world.add_user()
        challenge = captcha.make_arithmetic()
        run(gg._issue_row(world.group_id, user.user_id, challenge, 555, _expires_in(300)))  # noqa: SLF001

        assert run(gg._fetch_by_message(world.group_id, 556)) is None  # noqa: SLF001


class TestRecordWrongAttempt:
    def test_updates_the_attempts_counter_in_place(
        self, run: Callable[[Coroutine[Any, Any, Any]], Any], world: World
    ) -> None:
        user = world.add_user()
        challenge = captcha.make_arithmetic()
        run(gg._issue_row(world.group_id, user.user_id, challenge, 1, _expires_in(300)))  # noqa: SLF001

        run(gg._record_wrong_attempt(world.group_id, user.user_id, 3))  # noqa: SLF001

        row = run(gg._fetch_pending(world.group_id, user.user_id))  # noqa: SLF001
        assert row["attempts"] == 3
        # nonce/answer untouched — a wrong attempt does not reissue the challenge.
        assert row["nonce"] == challenge.nonce


class TestDeleteChallenge:
    def test_removes_the_row(
        self, run: Callable[[Coroutine[Any, Any, Any]], Any], world: World
    ) -> None:
        user = world.add_user()
        challenge = captcha.make_arithmetic()
        run(gg._issue_row(world.group_id, user.user_id, challenge, 1, _expires_in(300)))  # noqa: SLF001

        run(gg._delete_challenge(world.group_id, user.user_id))  # noqa: SLF001

        assert run(gg._fetch_pending(world.group_id, user.user_id)) is None  # noqa: SLF001

    def test_is_a_noop_when_nothing_is_pending(
        self, run: Callable[[Coroutine[Any, Any, Any]], Any], world: World
    ) -> None:
        # Must not raise for a user with no row (both the solved and the
        # kicked path call this unconditionally).
        run(gg._delete_challenge(world.group_id, 424242))  # noqa: SLF001


class TestCascade:
    def test_row_is_deleted_when_the_group_is_deleted(
        self, run: Callable[[Coroutine[Any, Any, Any]], Any], world: World, pg: ModuleType
    ) -> None:
        """`captcha_challenges.group_id` FK is `ON DELETE CASCADE` (migration 0001)."""
        user = world.add_user()
        challenge = captcha.make_arithmetic()
        run(gg._issue_row(world.group_id, user.user_id, challenge, 1, _expires_in(300)))  # noqa: SLF001

        run(
            pg.execute(
                "DELETE FROM groups WHERE group_id = $1", world.group_id, name="test_drop_group"
            )
        )

        row = run(
            pg.fetchrow(
                "SELECT 1 FROM captcha_challenges WHERE group_id = $1",
                world.group_id,
                name="test_captcha_row_gone",
            )
        )
        assert row is None
        # world.teardown()'s own `DELETE FROM groups` is a no-op from here —
        # same as qa/integration/test_group_welcomes.py's identical cascade test.


class TestCitusTopology:
    """The read/write queries must touch exactly one shard — AGENTS.md §4 rule 6."""

    def _task_count(
        self,
        run: Callable[[Coroutine[Any, Any, Any]], Any],
        citus: ModuleType,
        sql: str,
        *args: object,
    ) -> int | None:
        plan = run(citus.fetch(f"EXPLAIN (COSTS OFF) {sql}", *args))
        for row in plan:
            line = str(row[0]).strip()
            if line.startswith("Task Count:"):
                return int(line.split(":")[1])
        return None

    def test_fetch_pending_is_single_shard(
        self, citus: ModuleType, run: Callable[[Coroutine[Any, Any, Any]], Any], world: World
    ) -> None:
        task_count = self._task_count(
            run,
            citus,
            "SELECT nonce, kind, answer, attempts, message_id, expires_at "
            "FROM captcha_challenges WHERE group_id = $1 AND user_id = $2 AND solved_at IS NULL",
            world.group_id,
            1,
        )
        assert task_count == 1

    def test_fetch_by_message_is_single_shard(
        self, citus: ModuleType, run: Callable[[Coroutine[Any, Any, Any]], Any], world: World
    ) -> None:
        task_count = self._task_count(
            run,
            citus,
            "SELECT user_id, nonce, answer, attempts, expires_at "
            "FROM captcha_challenges WHERE group_id = $1 AND message_id = $2 AND solved_at IS NULL",
            world.group_id,
            1,
        )
        assert task_count == 1
