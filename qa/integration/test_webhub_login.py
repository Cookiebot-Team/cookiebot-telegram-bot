"""`cb_api.keys` against a real database — the D7 regression.

v1 generated its RSA signing key at module import
(`../COOKIEBOT-Telegram-Group-Bot/Bot/Server.py:23-24`), so it lived and died
with the process — and `run_api_server` starts **two** gunicorn workers
(`:112`), so at any moment two different keys were signing tokens while
`/.well-known/jwks.json` published whichever one answered that request. A
token was verifiable roughly half the time, and never after a restart.

Nothing about that is provable without a shared store, which is why these live
here rather than next to the router's own tests. `keys.reset_cache()` is what
stands in for "a new process": the module-level cache is the only thing a
restart clears.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

import jwt
import pytest

from cb_api import auth, keys
from cb_core import db
from cb_core.settings import Settings

pytestmark = pytest.mark.integration

Run = Callable[[Coroutine[Any, Any, Any]], Any]

KID = "cb-test-key"


@pytest.fixture(autouse=True)
def _isolated_key(database: object, monkeypatch: pytest.MonkeyPatch, run: Run) -> Any:
    """A kid of this test's own, and no configured PEM — the unconfigured
    deployment, which is the one D7 actually bit."""
    settings = Settings(service_name="cb-api-test", traces_enabled=False, webhub_jwt_kid=KID)
    monkeypatch.setattr(keys, "get_settings", lambda: settings)
    run(db.execute("DELETE FROM signing_keys WHERE kid = $1", KID, name="test_clean_key"))
    keys.reset_cache()
    yield settings
    run(db.execute("DELETE FROM signing_keys WHERE kid = $1", KID, name="test_clean_key"))
    keys.reset_cache()


class TestDurability:
    def test_the_key_is_generated_once_and_persisted(self, run: Run) -> None:
        first = run(keys.signing_key())
        row = run(db.fetchrow("SELECT private_pem FROM signing_keys WHERE kid = $1", KID))
        assert row is not None
        assert row["private_pem"] == first.private_pem

    def test_a_restart_keeps_the_same_key(self, run: Run) -> None:
        """The D7 regression proper: v1 answered this with a different key
        every time, so every token issued before the restart stopped
        verifying."""
        first = run(keys.signing_key())
        keys.reset_cache()  # a new process
        assert run(keys.signing_key()).private_pem == first.private_pem

    def test_a_token_issued_before_a_restart_still_verifies_after_it(self, run: Run) -> None:
        before = run(keys.signing_key())
        claims = auth.build_claims(subject="7", issuer="i", kid=before.kid, ttl_seconds=1800)
        token = auth.issue_token(claims, before.private_pem, before.kid)

        keys.reset_cache()
        published = run(keys.published_keys())
        jwks = {"keys": [keys.public_jwk(k.private_pem, k.kid) for k in published]}
        key = jwt.PyJWKSet.from_dict(jwks)[before.kid]
        assert jwt.decode(token, key.key, algorithms=["RS256"])["sub"] == "7"

    def test_a_replica_that_loses_the_insert_race_adopts_the_winners_key(self, run: Run) -> None:
        """Two cold replicas both generate a key and one `INSERT` wins. The
        loser must sign with the row that is actually in the table — keeping
        its own would publish a JWKS that cannot verify its own tokens, which
        is D7 by another route.
        """
        winner = keys.generate_private_pem()
        run(
            db.execute(
                "INSERT INTO signing_keys (kid, algorithm, private_pem) VALUES ($1, $2, $3)",
                KID,
                keys.ALGORITHM,
                winner,
                name="test_seed_winner",
            )
        )
        keys.reset_cache()
        assert run(keys.signing_key()).private_pem == winner

    def test_a_configured_pem_never_touches_the_table(
        self, _isolated_key: Settings, run: Run
    ) -> None:
        """The escape hatch for a deployment that does not want a private key
        in its application database."""
        _isolated_key.webhub_jwt_private_key_pem = keys.generate_private_pem()
        keys.reset_cache()
        assert run(keys.signing_key()).private_pem == _isolated_key.webhub_jwt_private_key_pem
        assert run(db.fetchrow("SELECT 1 FROM signing_keys WHERE kid = $1", KID)) is None


class TestTopology:
    def test_signing_keys_is_a_reference_table(self, run: Run) -> None:
        """It has no `group_id` and is read on every login. A distributed table
        would need one; a local table would be invisible to the other nodes."""
        row = run(
            db.fetchrow(
                """
                SELECT partmethod::text AS partmethod FROM pg_dist_partition
                WHERE logicalrelid = 'signing_keys'::regclass
                """
            )
        )
        assert row is not None, "signing_keys is not in pg_dist_partition at all"
        # `partmethod` is a `"char"`, which asyncpg hands back as bytes unless
        # it is cast — 'n' is Citus' code for a reference table.
        assert row["partmethod"] == "n"
