"""Contract: every response matches the shape `openapi.json` promises.

A contract test is not a behaviour test. It does not care whether the number is
right — `test_integration.py` does — only that the *shape* is the one the
published document describes, because that document is what the Mini App's
client is generated from and what anyone writing against this API reads first.

Three properties, in the order they matter:

1. **The document describes the app.** The committed
   `docs/site/public/openapi.json` is regenerated from the app and checked in;
   this asserts the two still agree, so the rest of the file is validating
   against something true.
2. **Every declared operation is exercised.** `CASES` is a whitelist, not a
   sample — a new endpoint fails collection until someone adds a row, the same
   rule `packages/cb-api/tests/test_openapi.py` applies to the document itself.
3. **Every response validates.** Successes against their response model,
   refusals against theirs, with `jsonschema-rs` reporting *all* the ways a
   payload drifted rather than the first.

Validation is against the **committed artifact**, never against the document the
app would build right now — an app checked against its own live description
agrees with itself by construction and can change shape freely.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from qa.api import schemas
from qa.api.client import Api, Tokens
from qa.integration.factories import World

pytestmark = [pytest.mark.contract, pytest.mark.integration]


@dataclass(frozen=True)
class Case:
    """One documented operation, and how to call it successfully.

    `role` is the *lowest-privileged* caller who should succeed. Using an owner
    everywhere would pass and prove less: a group admin succeeding at
    `/groups/{id}/config` is part of the contract.
    """

    method: str
    path: str
    role: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    json: dict[str, Any] | None = None
    expect: int = 200

    @property
    def id(self) -> str:
        return f"{self.method.upper()} {self.path}"


#: Every operation in the document. `test_every_documented_operation_has_a_case`
#: fails when this list and `openapi.json` disagree, in either direction.
CASES: tuple[Case, ...] = (
    Case("get", "/healthz"),
    # `/readyz` reports on Valkey and object storage, which this suite
    # deliberately does not start (see the conftest). Its *shape* is what is
    # under test here, and the 503 body is the same model as the 200.
    Case("get", "/readyz", expect=503),
    Case("get", "/"),
    Case("get", "/.well-known/jwks.json"),
    Case("get", "/.well-known/openid-configuration"),
    # `/login` and `/oauth2/token` are driven with real signed payloads in
    # `test_integration.py`; here only the refusal shape is contractual, since a
    # success body is asserted there against the token it actually issues.
    Case("post", "/login", json={"id": 1, "auth_date": 1, "hash": "no"}, expect=401),
    Case("post", "/oauth2/token", json={"grant_type": "nonsense"}, expect=400),
    Case("post", "/oauth2/revoke", json={"token": "never-issued"}),
    Case("get", "/me", role="admin"),
    Case("get", "/groups/{group_id}/config", role="admin"),
    Case("patch", "/groups/{group_id}/config", role="admin", json={"sfw": False}),
    Case("get", "/groups/{group_id}/rules", role="admin"),
    Case("put", "/groups/{group_id}/rules", role="admin", json={"body": "be kind"}),
    Case("get", "/groups/{group_id}/welcome", role="admin"),
    Case("put", "/groups/{group_id}/welcome", role="admin", json={"body": "hello <user>"}),
    Case("get", "/groups/{group_id}/audit", role="admin"),
    Case("get", "/groups/{group_id}/analytics/daily", role="admin"),
    Case("get", "/groups/{group_id}/analytics/commands", role="admin"),
    Case("get", "/groups/{group_id}/analytics/llm", role="admin"),
    Case("get", "/groups/{group_id}/analytics/summary", role="admin"),
    Case("get", "/admin/overview", role="owner"),
    Case("get", "/admin/analytics/daily", role="owner"),
    Case("get", "/admin/analytics/groups", role="owner"),
    Case("get", "/admin/analytics/commands", role="owner"),
    Case("get", "/admin/analytics/llm", role="owner"),
    Case("get", "/admin/groups", role="owner"),
    Case("get", "/admin/tenant", role="owner"),
)


def call(api: Api, case: Case, tokens: Tokens, group: World) -> Any:
    path = case.path.replace("{group_id}", str(group.group_id))
    token = getattr(tokens, case.role) if case.role else None
    return api.request(case.method, path, token=token, json=case.json, params=case.params or None)


def assert_matches(case: Case, status: int, payload: Any) -> None:
    schema = schemas.response_schema(case.method, case.path, status)
    if schema is None:
        pytest.fail(
            f"{case.id} answered {status}, which the document declares no body for — "
            "either the handler or `responses=` on the route is wrong"
        )
    errors = schemas.errors_against(schema, payload)
    assert not errors, f"{case.id} → {status} does not match its schema:\n  " + "\n  ".join(errors)


# ------------------------------------------------------------- the document


def test_the_published_document_matches_the_app() -> None:
    """The committed artifact is what everything else here validates against,
    so it has to be the app's own document — not one someone edited."""
    from cb_api.main import app

    live = app.openapi()
    published = schemas.spec()
    assert live == published, (
        "docs/site/public/openapi.json is stale — run `python scripts/cb.py api-docs`"
    )


def test_every_documented_operation_has_a_case() -> None:
    """A whitelist, not a sample: an endpoint added without a row here would
    ship with nothing validating its shape."""
    documented = {(method, path) for method, path in schemas.paths_with_schema()}
    covered = {(case.method, case.path) for case in CASES}
    assert documented - covered == set(), "documented but never called by a contract test"
    assert covered - documented == set(), "called by a contract test but not in the document"


# -------------------------------------------------------------- the shapes


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.id)
def test_the_response_matches_its_declared_schema(
    api: Api, tokens: Tokens, group: World, case: Case
) -> None:
    response = call(api, case, tokens, group)

    assert response.status_code == case.expect, response.text
    assert response.headers["content-type"].startswith("application/json")
    assert_matches(case, response.status_code, response.json())


@pytest.mark.parametrize("case", [case for case in CASES if case.role], ids=lambda case: case.id)
def test_the_401_body_matches_its_declared_schema(
    api: Api, tokens: Tokens, group: World, case: Case
) -> None:
    """A refusal is part of the contract too. A client that cannot parse the
    401 it was given cannot tell "log in again" from "the server broke"."""
    response = call(api, Case(case.method, case.path, None, case.params, case.json), tokens, group)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert_matches(case, 401, response.json())


@pytest.mark.parametrize(
    "case",
    [case for case in CASES if "{group_id}" in case.path],
    ids=lambda case: case.id,
)
def test_the_404_body_matches_its_declared_schema(
    api: Api, tokens: Tokens, group: World, case: Case
) -> None:
    """A stranger gets 404 rather than 403, and it is a documented shape rather
    than whatever FastAPI happened to raise."""
    response = api.request(
        case.method,
        case.path.replace("{group_id}", str(group.group_id)),
        token=tokens.stranger,
        json=case.json,
    )

    assert response.status_code == 404
    assert_matches(case, 404, response.json())


@pytest.mark.parametrize(
    "case", [case for case in CASES if case.path.startswith("/admin")], ids=lambda case: case.id
)
def test_the_403_body_matches_its_declared_schema(
    api: Api, tokens: Tokens, group: World, case: Case
) -> None:
    """The fleet-wide endpoints refuse a group admin with 403, and say so in a
    shape a client can read."""
    response = api.request(case.method, case.path, token=tokens.admin)

    assert response.status_code == 403
    assert_matches(case, 403, response.json())


def test_a_rejected_value_is_a_documented_422(api: Api, tokens: Tokens, group: World) -> None:
    """FastAPI's validation envelope is in the document because clients meet it
    — a bounded field is only a contract if the refusal has a shape."""
    response = api.patch(
        f"/groups/{group.group_id}/config",
        token=tokens.admin,
        json={"captcha_timeout_seconds": 10**9},
    )

    assert response.status_code == 422
    assert_matches(Case("patch", "/groups/{group_id}/config"), 422, response.json())
