"""x_miniapp_auth — refresh tokens, and what happens when one is replayed.

An access token is a short-lived JWT nothing stores. A refresh token is the
opposite: long-lived, and therefore worth stealing. Three decisions follow, and
they are the whole module.

**Only a hash is stored.** `sha256(token)` goes to the database; the token is
returned once. A dump of `refresh_tokens` is not a set of sessions.

**Every refresh rotates.** Redeeming a token marks it used and issues a new one
in the same *family*. A client that keeps working keeps exactly one live token.

**A replayed token kills its family.** If a token that was already redeemed
comes back, either the client is buggy or someone copied it — and there is no
way to tell which from here. Revoking every token in the family logs both the
attacker and the legitimate client out, which is the recoverable failure; the
alternative is letting a thief refresh forever beside the real user.

Opaque and random rather than a second JWT: a refresh token needs to be
revocable, and a self-contained token that verifies offline is exactly what
cannot be revoked.
"""

from __future__ import annotations

import dataclasses
import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

from cb_core import db, ids, metrics
from cb_core.logging import get_logger

log = get_logger("cb.api.sessions")

#: 32 bytes from `secrets` — 256 bits of entropy, URL-safe so it survives a
#: form post and a JSON body without escaping.
_TOKEN_BYTES = 32

_INSERT = """
INSERT INTO refresh_tokens (token_hash, family_id, user_id, scope, audience, issued_at, expires_at)
VALUES ($1, $2, $3, $4, $5, $6, $7)
"""

_SELECT = """
SELECT token_hash, family_id, user_id, scope, audience, issued_at, expires_at, used_at, revoked_at
  FROM refresh_tokens
 WHERE token_hash = $1
"""

_MARK_USED = """
UPDATE refresh_tokens
   SET used_at = $2
 WHERE token_hash = $1
   AND used_at IS NULL
   AND revoked_at IS NULL
"""

_REVOKE_FAMILY = """
UPDATE refresh_tokens
   SET revoked_at = $2
 WHERE family_id = $1
   AND revoked_at IS NULL
"""

_REVOKE_ONE = """
UPDATE refresh_tokens
   SET revoked_at = $2
 WHERE token_hash = $1
   AND revoked_at IS NULL
"""

_PURGE = "DELETE FROM refresh_tokens WHERE expires_at < $1"


@dataclasses.dataclass(frozen=True, slots=True)
class Session:
    """A refresh token's row. Never carries the token itself."""

    family_id: UUID
    user_id: int
    scope: str
    audience: str
    issued_at: datetime
    expires_at: datetime
    used_at: datetime | None = None
    revoked_at: datetime | None = None

    def is_live(self, *, now: datetime | None = None) -> bool:
        reference = now or datetime.now(UTC)
        return self.revoked_at is None and self.used_at is None and self.expires_at > reference


@dataclasses.dataclass(frozen=True, slots=True)
class IssuedRefresh:
    """What the caller hands back to the client, once."""

    token: str
    session: Session


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def issue(
    *,
    user_id: int,
    scope: str,
    audience: str,
    ttl_seconds: int,
    family_id: UUID | None = None,
    now: datetime | None = None,
) -> IssuedRefresh:
    """Mint a refresh token, continuing `family_id` when this is a rotation."""
    issued_at = now or datetime.now(UTC)
    session = Session(
        family_id=family_id or ids.uuid7(),
        user_id=user_id,
        scope=scope,
        audience=audience,
        issued_at=issued_at,
        expires_at=issued_at + timedelta(seconds=ttl_seconds),
    )
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    await db.execute(
        _INSERT,
        token_hash(token),
        session.family_id,
        session.user_id,
        session.scope,
        session.audience,
        session.issued_at,
        session.expires_at,
        name="refresh_token_insert",
    )
    return IssuedRefresh(token=token, session=session)


async def load(token: str) -> Session | None:
    row = await db.fetchrow(_SELECT, token_hash(token), name="refresh_token_lookup")
    if row is None:
        return None
    return Session(
        family_id=row["family_id"],
        user_id=row["user_id"],
        scope=row["scope"],
        audience=row["audience"],
        issued_at=row["issued_at"],
        expires_at=row["expires_at"],
        used_at=row["used_at"],
        revoked_at=row["revoked_at"],
    )


async def redeem(token: str, *, now: datetime | None = None) -> Session | None:
    """Spend a refresh token exactly once.

    Returns the session it belonged to, or `None` for every failure — unknown,
    expired, revoked, or already spent. The caller must not distinguish them to
    the client: which one it was is information about somebody else's token.

    The single-use guarantee is the `used_at IS NULL` predicate on the UPDATE,
    not a read followed by a write: two refreshes racing the same token both
    read "unused", and only the one whose UPDATE reports a row may proceed.
    """
    reference = now or datetime.now(UTC)
    session = await load(token)
    if session is None:
        return None

    if session.used_at is not None:
        # Replay. Kill the family — see the module docstring.
        metrics.auth_refresh_reuse_total.inc()
        log.warning("auth.refresh_reused", user_id=session.user_id)
        await revoke_family(session.family_id, now=reference)
        return None

    if session.revoked_at is not None or session.expires_at <= reference:
        return None

    result = await db.execute(
        _MARK_USED, token_hash(token), reference, name="refresh_token_mark_used"
    )
    if result.endswith(" 0"):
        # Another request rotated it between the read and here.
        metrics.auth_refresh_reuse_total.inc()
        await revoke_family(session.family_id, now=reference)
        return None
    return dataclasses.replace(session, used_at=reference)


async def revoke(token: str, *, now: datetime | None = None) -> bool:
    """RFC 7009: revoking an unknown token is a success, not an error — the
    caller's goal ("this token cannot be used") holds either way."""
    result = await db.execute(
        _REVOKE_ONE, token_hash(token), now or datetime.now(UTC), name="refresh_token_revoke"
    )
    return not result.endswith(" 0")


async def revoke_family(family_id: UUID, *, now: datetime | None = None) -> None:
    await db.execute(
        _REVOKE_FAMILY, family_id, now or datetime.now(UTC), name="refresh_token_revoke_family"
    )


async def purge_expired(*, now: datetime | None = None) -> None:
    """Drop rows past their expiry. A revoked-but-unexpired row stays: it is
    what turns a replay into a detected replay rather than an unknown token."""
    await db.execute(_PURGE, now or datetime.now(UTC), name="refresh_token_purge")


__all__ = [
    "IssuedRefresh",
    "Session",
    "issue",
    "load",
    "purge_expired",
    "redeem",
    "revoke",
    "revoke_family",
    "token_hash",
]
