"""What a refusal looks like on the wire, in one place.

Three routers were each declaring their own dictionary of 401/403/404
descriptions, which meant three near-copies of the same sentences and — until
`qa/api/test_contract.py` said so — two of them naming no model at all, so the
document promised a status with no body while the service returned one.

A refusal is part of the contract. A client that cannot parse the 401 it was
given cannot tell "log in again" from "the server broke", and a generated client
gets `unknown` for the one branch it most needs to handle.

The wording of each entry is the *reason* for that status here, not a generic
gloss: the difference between the 403 a group endpoint answers and the 404 it
answers instead of 403 is the single most surprising thing about this API, and
the place a reader meets it is the schema.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ErrorBody(BaseModel):
    """FastAPI's error envelope — `{"detail": "..."}` — named so the schema can
    point at it and a generated client can read it."""

    detail: str = Field(description="a sentence for a developer, never for an end user")


#: A missing or unverifiable bearer token. Always accompanied by a
#: `WWW-Authenticate: Bearer` challenge, which is what tells a browser to log in
#: again rather than to show an error.
UNAUTHORIZED: dict[str | int, dict[str, Any]] = {
    401: {"model": ErrorBody, "description": "no bearer token, or one that did not verify"}
}

#: A caller who administers the group but whose token lacks the scope. Fixable
#: by asking `/oauth2/token` for a better one, and the challenge names which.
FORBIDDEN_SCOPE: dict[str | int, dict[str, Any]] = {
    403: {
        "model": ErrorBody,
        "description": "an admin whose token lacks the scope; ask /oauth2/token for a better one",
    }
}

#: The group-scoped refusal. Deliberately the same answer an unknown group gets,
#: so a logged-in stranger cannot walk chat ids.
NOT_FOUND_OR_NOT_ADMIN: dict[str | int, dict[str, Any]] = {
    404: {
        "model": ErrorBody,
        "description": "no such group — or the caller does not administer it, which answers alike",
    }
}

#: The fleet-wide refusal. 403 rather than 404 because `/admin/...` is a fixed
#: path in this document: there is no chat id to hide, and a 404 would only
#: mislead an owner holding a stale token.
NOT_AN_OWNER: dict[str | int, dict[str, Any]] = {
    403: {
        "model": ErrorBody,
        "description": "not an owner of this deployment — or an owner whose token lacks the scope",
    }
}

#: A window the endpoint will not answer for. A 400, never a silent clamp: a
#: caller that asked for the wrong window should learn that rather than get
#: plausible numbers for a window it did not request.
BAD_WINDOW: dict[str | int, dict[str, Any]] = {
    400: {"model": ErrorBody, "description": "the window is reversed or longer than a year"}
}


def group_errors(*extra: dict[str | int, dict[str, Any]]) -> dict[str | int, dict[str, Any]]:
    """The three a group-scoped endpoint can answer with, plus anything else it
    adds."""
    merged: dict[str | int, dict[str, Any]] = {
        **UNAUTHORIZED,
        **FORBIDDEN_SCOPE,
        **NOT_FOUND_OR_NOT_ADMIN,
    }
    for entry in extra:
        merged.update(entry)
    return merged


def fleet_errors(*extra: dict[str | int, dict[str, Any]]) -> dict[str | int, dict[str, Any]]:
    """The two a fleet-wide endpoint can answer with. Note the absence of 404 —
    asserted by `packages/cb-api/tests/test_openapi.py`."""
    merged: dict[str | int, dict[str, Any]] = {**UNAUTHORIZED, **NOT_AN_OWNER}
    for entry in extra:
        merged.update(entry)
    return merged


__all__ = [
    "BAD_WINDOW",
    "FORBIDDEN_SCOPE",
    "NOT_AN_OWNER",
    "NOT_FOUND_OR_NOT_ADMIN",
    "UNAUTHORIZED",
    "ErrorBody",
    "fleet_errors",
    "group_errors",
]
