"""The RSA key the web console's tokens are signed with — one, shared, durable.

`x_webhub_login`, FEATURE-MAP **D7**. Migration `0008`'s docstring has the full
account of what v1 did (a fresh key per process at import, two gunicorn workers,
a JWKS that described one of them). What matters here is the resolution order:

1. `CB_WEBHUB_JWT_PRIVATE_KEY_PEM`, if set — nothing is read or written.
2. the `signing_keys` row for `CB_WEBHUB_JWT_KID`, if it exists.
3. otherwise generate one, `INSERT ... ON CONFLICT DO NOTHING`, and **read back
   what is actually in the table**. Two replicas starting cold at the same
   moment both generate a key and one insert loses; the loser must adopt the
   winner's key rather than keep the one it made, or it signs tokens the
   published JWKS cannot verify — which is D7 again, by a different route.

The result is cached per process. A rotation (delete the row, change the kid,
restart) is deliberately not a hot-reload: the JWKS publishes every row in the
table, so an overlapping rotation is a `signing_keys` insert plus a rolling
restart, and nothing has to invalidate a cache it cannot see.
"""

from __future__ import annotations

import json
from typing import Any

import msgspec
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from cb_core import db
from cb_core.logging import get_logger
from cb_core.settings import get_settings

log = get_logger("cb.api.keys")

#: v1 generated 2048-bit RS256 keys (`Server.py:23`).
KEY_SIZE = 2048
ALGORITHM = "RS256"

_cached: SigningKey | None = None


class SigningKey(msgspec.Struct, frozen=True):
    kid: str
    private_pem: str


def load_private_key(private_pem: str) -> rsa.RSAPrivateKey:
    """Parse a PEM and insist it is RSA.

    `load_pem_private_key` returns the union of every key type `cryptography`
    supports, and every path here signs RS256. An operator who puts an Ed25519
    key in `CB_WEBHUB_JWT_PRIVATE_KEY_PEM` should find out at startup, from
    this message, rather than through a token nothing can verify.
    """
    key = serialization.load_pem_private_key(private_pem.encode(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise TypeError(f"{ALGORITHM} needs an RSA private key, got {type(key).__name__}")
    return key


def generate_private_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=KEY_SIZE)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


def public_jwk(private_pem: str, kid: str) -> dict[str, Any]:
    """The public half as a JWK, the shape `/.well-known/jwks.json` publishes.

    v1 exported `jwcrypto`'s own JWK (`Server.py:80-84`), which carries exactly
    these fields for an RSA signing key. Built by hand here rather than adding
    `jwcrypto` for one export — `PyJWT` already ships the algorithm that does
    it, and it is the library this module signs with.
    """
    jwk: dict[str, Any] = json.loads(
        RSAAlgorithm.to_jwk(load_private_key(private_pem).public_key())
    )
    jwk.update({"kid": kid, "alg": ALGORITHM, "use": "sig"})
    return jwk


def public_pem(private_pem: str) -> str:
    """The public half as PEM — what `jwt.decode` verifies against.

    `public_jwk` above is for publishing; this is for verifying locally
    (`cb_api.security`), and deriving both from the same stored private key is
    what keeps the two from ever disagreeing about which key a `kid` names.
    """
    return (
        load_private_key(private_pem)
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )


_SELECT = "SELECT kid, private_pem FROM signing_keys WHERE kid = $1"
_INSERT = """
INSERT INTO signing_keys (kid, algorithm, private_pem)
VALUES ($1, $2, $3)
ON CONFLICT (kid) DO NOTHING
"""
_SELECT_ALL = "SELECT kid, private_pem FROM signing_keys ORDER BY created_at"


async def signing_key() -> SigningKey:
    """The key this process signs with. Resolution order in the module docstring."""
    global _cached
    if _cached is not None:
        return _cached

    settings = get_settings()
    if settings.webhub_jwt_private_key_pem:
        _cached = SigningKey(
            kid=settings.webhub_jwt_kid, private_pem=settings.webhub_jwt_private_key_pem
        )
        return _cached

    kid = settings.webhub_jwt_kid
    row = await db.fetchrow(_SELECT, kid, name="signing_key_read")
    if row is None:
        await db.execute(_INSERT, kid, ALGORITHM, generate_private_pem(), name="signing_key_write")
        # Read back rather than trusting the generated value: on a lost insert
        # race the table holds the other replica's key, and that is the one the
        # JWKS will publish.
        row = await db.fetchrow(_SELECT, kid, name="signing_key_reread")
        if row is None:  # pragma: no cover - the row was just written
            raise RuntimeError("signing key vanished immediately after being written")
        log.info("signing_key.resolved", kid=kid)

    _cached = SigningKey(kid=row["kid"], private_pem=row["private_pem"])
    return _cached


async def published_keys() -> tuple[SigningKey, ...]:
    """Every key a consumer might have to verify against — the JWKS body.

    More than one when a rotation is in flight, and exactly one (never read
    from the database) when the PEM comes from configuration.
    """
    settings = get_settings()
    if settings.webhub_jwt_private_key_pem:
        return (await signing_key(),)
    rows = await db.fetch(_SELECT_ALL, name="signing_keys_all")
    if not rows:
        return (await signing_key(),)
    return tuple(SigningKey(kid=r["kid"], private_pem=r["private_pem"]) for r in rows)


def reset_cache() -> None:
    """Tests only: forget the resolved key so the next call resolves again."""
    global _cached
    _cached = None


__all__ = [
    "ALGORITHM",
    "KEY_SIZE",
    "SigningKey",
    "generate_private_pem",
    "load_private_key",
    "public_jwk",
    "public_pem",
    "published_keys",
    "reset_cache",
    "signing_key",
]
