"""The published schema, as a contract.

The Mini App and the web console are built from `/openapi.json` — a generated
client is only as good as the document it was generated from, and a route that
answers `{"type": "object"}` hands its author a `Record<string, unknown>` and a
guess. So the shapes are asserted here rather than trusted to whoever adds the
next endpoint.

The two exemptions are v1's: `/` and `/login` predate this API and their bodies
are what `COOKIEBOT-WebHub` reads by name; a `response_model` on either would
filter v1's error bodies back out, which is the one break this codebase does not
allow (`cb_api.routers.login`).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from cb_api.main import app

#: Everything a Mini App session touches, in the order it touches them.
MINIAPP_PATHS = (
    ("get", "/.well-known/openid-configuration"),
    ("get", "/.well-known/jwks.json"),
    ("post", "/oauth2/token"),
    ("post", "/oauth2/revoke"),
    ("get", "/me"),
    ("get", "/groups/{group_id}/config"),
    ("patch", "/groups/{group_id}/config"),
    ("get", "/groups/{group_id}/rules"),
    ("put", "/groups/{group_id}/rules"),
    ("get", "/groups/{group_id}/welcome"),
    ("put", "/groups/{group_id}/welcome"),
    ("get", "/groups/{group_id}/audit"),
    ("get", "/groups/{group_id}/analytics/daily"),
    ("get", "/groups/{group_id}/analytics/commands"),
    ("get", "/groups/{group_id}/analytics/llm"),
    ("get", "/groups/{group_id}/analytics/summary"),
    # x_admin_api: only an owner's session ever calls these, but the Mini App
    # is one generated client and the document has to describe them all.
    ("get", "/admin/overview"),
    ("get", "/admin/analytics/daily"),
    ("get", "/admin/analytics/groups"),
    ("get", "/admin/analytics/commands"),
    ("get", "/admin/analytics/llm"),
    ("get", "/admin/groups"),
    ("get", "/admin/tenant"),
)

#: The fleet-wide half. Split out so the assertion below can say what is true of
#: these and of nothing else: **403**, never 404. The group endpoints hide
#: behind a 404 because `{group_id}` is worth hiding; `/admin/...` is a fixed
#: path in this very document, so a 404 there would only mislead an owner
#: holding the wrong token.
ADMIN_PATHS = tuple(entry for entry in MINIAPP_PATHS if entry[1].startswith("/admin"))

#: v1's two, exempt for the reason in this module's docstring.
UNMODELLED = {("get", "/"), ("post", "/login")}


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    return app.openapi()


def _operation(spec: dict[str, Any], method: str, path: str) -> dict[str, Any]:
    assert path in spec["paths"], f"{path} is not in the schema"
    operation = spec["paths"][path].get(method)
    assert operation is not None, f"{method.upper()} {path} is not in the schema"
    return operation


@pytest.mark.parametrize(("method", "path"), MINIAPP_PATHS)
def test_every_miniapp_response_names_a_schema(
    spec: dict[str, Any], method: str, path: str
) -> None:
    operation = _operation(spec, method, path)
    schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert "$ref" in json.dumps(schema), f"{method.upper()} {path} answers an undescribed object"


@pytest.mark.parametrize(("method", "path"), MINIAPP_PATHS)
def test_every_miniapp_operation_is_summarised(
    spec: dict[str, Any], method: str, path: str
) -> None:
    """A generated client turns `summary` into the method's own docstring."""
    assert _operation(spec, method, path).get("summary")


@pytest.mark.parametrize(
    ("method", "path"),
    [entry for entry in MINIAPP_PATHS if entry[1].startswith(("/me", "/groups"))],
)
def test_the_group_endpoints_declare_their_refusals(
    spec: dict[str, Any], method: str, path: str
) -> None:
    """401 always; 403 and 404 wherever a group is named. A client that cannot
    tell "log in again" from "you do not administer this" will retry the wrong
    one — the distinction is the whole reason `security.group_admin_caller`
    orders its checks the way it does."""
    responses = _operation(spec, method, path)["responses"]
    assert "401" in responses
    if "{group_id}" in path:
        assert "404" in responses


@pytest.mark.parametrize(("method", "path"), ADMIN_PATHS)
def test_the_admin_endpoints_refuse_with_403_not_404(
    spec: dict[str, Any], method: str, path: str
) -> None:
    responses = _operation(spec, method, path)["responses"]
    assert "401" in responses
    assert "403" in responses
    assert "404" not in responses


def test_the_token_endpoint_documents_both_content_types() -> None:
    """RFC 6749 says form-encoded and the Mini App sends JSON; the handler takes
    either, so the schema has to say so."""
    body = app.openapi()["paths"]["/oauth2/token"]["post"]["requestBody"]["content"]
    assert set(body) == {"application/json", "application/x-www-form-urlencoded"}
    assert "grant_type" in body["application/json"]["schema"]["properties"]


def test_the_token_endpoint_documents_its_error_body(spec: dict[str, Any]) -> None:
    error = spec["paths"]["/oauth2/token"]["post"]["responses"]["400"]
    schema = error["content"]["application/json"]["schema"]
    assert schema["$ref"].endswith("TokenError")


def test_the_bearer_scheme_is_published(spec: dict[str, Any]) -> None:
    assert "HTTPBearer" in spec["components"]["securitySchemes"]
    assert spec["paths"]["/me"]["get"]["security"]


def test_no_route_grows_an_undescribed_body_unnoticed(spec: dict[str, Any]) -> None:
    """The list above is a whitelist, not a sample: a new endpoint lands here
    the day it is added, or this fails."""
    described = {(method, path) for method, path in MINIAPP_PATHS} | UNMODELLED
    health = {("get", "/healthz"), ("get", "/readyz")}
    everything = {(method, path) for path, methods in spec["paths"].items() for method in methods}
    assert everything - described - health == set()
