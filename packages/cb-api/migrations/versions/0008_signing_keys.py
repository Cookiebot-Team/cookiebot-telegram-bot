"""one signing key for the whole deployment, not one per process

`x_webhub_login` — FEATURE-MAP **D7**. v1's API server generated its RSA
signing key at import time
(`../COOKIEBOT-Telegram-Group-Bot/Bot/Server.py:23-24`):

    public_key = jwk.JWK.generate(kty='RSA', size=2048, alg='RS256', ...)

so every restart invalidated every token already issued, and every consumer's
cached JWKS described a key that no longer existed. Under gunicorn it was worse
than a restart problem: `run_api_server` starts **two** workers (`:112`), each
importing the module and generating its own key, so which key signed a token
depended on which worker answered — and `/.well-known/jwks.json` published only
the key of whichever worker served *that* request. At any moment roughly half
of all live tokens could not be verified against the published JWKS.

The fix is that the key outlives the process and is shared by every replica.
This table is where a generated one lives. A deployment that sets
`CB_WEBHUB_JWT_PRIVATE_KEY_PEM` never reads it.

**It holds a private key.** That is a deliberate trade-off, taken because the
alternative for an unconfigured deployment is not "no key in the database", it
is "a different key per replica per restart" — D7 itself. Two consequences,
both intended: the row is written once and then only ever read, and an operator
who does not want it there has a supported way to avoid it (the env var above).
Rotation is `DELETE FROM signing_keys WHERE kid = ...` plus a new `kid`; the
JWKS publishes every row, so a rotation can overlap.

Reference table, not distributed: it has no `group_id`, it is read on the token
path of every login, and it holds at most a handful of rows — exactly what
`users`/`blacklist`/`bots` are (`0001_initial_schema.py:136`).

Revision ID: 0008
Revises: 0007
"""

from __future__ import annotations

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE signing_keys (
            kid         text PRIMARY KEY,
            algorithm   text NOT NULL,
            private_pem text NOT NULL,
            created_at  timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("SELECT create_reference_table('signing_keys')")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS signing_keys")
