"""`cb_api.sessions` against a real database.

The single-use guarantee is a SQL predicate (`used_at IS NULL` on the UPDATE),
not application logic, so it can only be proved here: two refreshes racing the
same token must produce exactly one winner, and the loser must be treated as a
replay rather than quietly succeeding.

The rest of the module's promises are equally storage-shaped — the token itself
is never in the table, an expired row cannot be redeemed, and revoking a family
takes every token in it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import Any

import pytest

from cb_api import sessions

pytestmark = pytest.mark.integration

Run = Callable[[Coroutine[Any, Any, Any]], Any]

USER_ID = 424_243
SCOPE = "groups:read groups:write"
AUDIENCE = "cookiebot-miniapp"


@pytest.fixture(autouse=True)
def _clean(pg: ModuleType, run: Run) -> Any:
    def _purge() -> None:
        run(
            pg.execute(
                "DELETE FROM refresh_tokens WHERE user_id = $1", USER_ID, name="test_cleanup"
            )
        )

    _purge()
    yield
    _purge()


def _issue(run: Run, **overrides: Any) -> sessions.IssuedRefresh:
    kwargs: dict[str, Any] = {
        "user_id": USER_ID,
        "scope": SCOPE,
        "audience": AUDIENCE,
        "ttl_seconds": 3600,
    }
    kwargs.update(overrides)
    return run(sessions.issue(**kwargs))


class TestStorage:
    def test_the_token_itself_is_never_stored(self, run: Run, pg: ModuleType) -> None:
        issued = _issue(run)
        row = run(
            pg.fetchrow(
                "SELECT token_hash FROM refresh_tokens WHERE user_id = $1",
                USER_ID,
                name="test_lookup",
            )
        )
        assert row is not None
        assert row["token_hash"] != issued.token
        assert row["token_hash"] == sessions.token_hash(issued.token)

    def test_an_unknown_token_loads_as_nothing(self, run: Run) -> None:
        assert run(sessions.load("never-issued")) is None


class TestRedemption:
    def test_a_fresh_token_redeems_once(self, run: Run) -> None:
        issued = _issue(run)
        redeemed = run(sessions.redeem(issued.token))
        assert redeemed is not None
        assert redeemed.user_id == USER_ID
        assert redeemed.scope == SCOPE

    def test_the_second_redemption_fails_and_revokes_the_family(self, run: Run) -> None:
        issued = _issue(run)
        rotated = _issue(run, family_id=issued.session.family_id)
        assert run(sessions.redeem(issued.token)) is not None

        assert run(sessions.redeem(issued.token)) is None  # the replay
        # …and the token the honest client is holding died with it.
        assert run(sessions.redeem(rotated.token)) is None

    def test_two_concurrent_redemptions_produce_one_winner(self, run: Run) -> None:
        """The race the `used_at IS NULL` predicate exists for."""
        issued = _issue(run)

        async def both() -> list[sessions.Session | None]:
            return list(
                await asyncio.gather(sessions.redeem(issued.token), sessions.redeem(issued.token))
            )

        results = run(both())
        assert sum(result is not None for result in results) == 1

    def test_an_expired_token_cannot_be_redeemed(self, run: Run) -> None:
        past = datetime.now(UTC) - timedelta(hours=2)
        issued = _issue(run, ttl_seconds=60, now=past)
        assert run(sessions.redeem(issued.token)) is None

    def test_a_revoked_token_cannot_be_redeemed(self, run: Run) -> None:
        issued = _issue(run)
        assert run(sessions.revoke(issued.token)) is True
        assert run(sessions.redeem(issued.token)) is None

    def test_revoking_an_unknown_token_reports_no_row(self, run: Run) -> None:
        """The endpoint answers 200 either way (RFC 7009); the store still tells
        the truth about whether anything was there."""
        assert run(sessions.revoke("never-issued")) is False


class TestFamilies:
    def test_revoking_a_family_takes_every_token_in_it(self, run: Run) -> None:
        first = _issue(run)
        second = _issue(run, family_id=first.session.family_id)
        unrelated = _issue(run)

        run(sessions.revoke_family(first.session.family_id))

        assert run(sessions.load(first.token)).revoked_at is not None
        assert run(sessions.load(second.token)).revoked_at is not None
        assert run(sessions.load(unrelated.token)).revoked_at is None

    def test_purge_drops_expired_rows_and_keeps_live_ones(self, run: Run) -> None:
        expired = _issue(run, ttl_seconds=60, now=datetime.now(UTC) - timedelta(hours=2))
        live = _issue(run)

        run(sessions.purge_expired())

        assert run(sessions.load(expired.token)) is None
        assert run(sessions.load(live.token)) is not None
