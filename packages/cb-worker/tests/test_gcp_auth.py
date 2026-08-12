"""Unit tests for `cb_worker.bucket_export.gcp_auth` — no real Google IAM,
storage or network anywhere.

`FakeHttp` and `FakeBucket` are in-memory stand-ins for the `HttpClient`/
`BucketIam` protocols this module's provisioning functions take as
parameters — the same "every Google call is an injectable seam" contract the
module's own docstring describes. `FakeBucket` wraps a real
`google.api_core.iam.Policy`, not a fake one: that object is a plain,
network-free data structure (bindings in, bindings out), so using the real
thing exercises the exact `policy.bindings` list/set shape
`_add_bucket_binding`/`_remove_bucket_binding` depend on, not a hand-rolled
approximation of it.
"""

from __future__ import annotations

import base64
import json
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar

import google.auth
import pytest
from google.api_core.iam import Policy
from google.auth.exceptions import DefaultCredentialsError

from cb_worker.bucket_export import gcp_auth

_FAKE_KEY_B64 = base64.b64encode(b'{"type": "service_account", "project_id": "proj"}').decode()

#: Duplicated from `gcp_auth._USERINFO_URL` rather than importing the private
#: constant (ruff SLF001) — it is the standard, publicly documented OAuth2
#: userinfo endpoint, not an implementation detail worth coupling a test to.
_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"


class _FakeResponse:
    def __init__(self, status_code: int, body: Mapping[str, object]) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> Mapping[str, object]:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}: {self._body}")


class FakeHttp:
    """Records every call so `--dry-run`-style tests can assert exactly
    nothing happened, and models the two IAM resources
    (`provision_export_account`/`revoke_export_account` touch: a project's
    service accounts, and one service account's own IAM policy."""

    def __init__(self, *, userinfo: Mapping[str, object] | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.sa_policies: dict[str, dict[str, Any]] = {}
        self.accounts: set[str] = set()
        self._userinfo = userinfo

    def get(self, url: str, *, params: Mapping[str, str] | None = None) -> _FakeResponse:
        # Deliberately narrow: on `iam.googleapis.com`, `:getIamPolicy` is a
        # POST-only custom method (see `gcp_auth._get_service_account_policy`'s
        # own docstring for why that is easy to get backwards) — this fake
        # raises rather than silently answering a `GET` to it, so a
        # regression back to the wrong verb fails the suite, not just a live
        # run three commits later.
        self.calls.append(("GET", url))
        if url == _USERINFO_URL:
            if self._userinfo is None:
                return _FakeResponse(403, {"error": {"message": "serviceusage boom"}})
            return _FakeResponse(200, self._userinfo)
        raise AssertionError(f"unexpected GET {url}")

    def post(self, url: str, *, json: Mapping[str, object] | None = None) -> _FakeResponse:
        self.calls.append(("POST", url))
        if url.endswith("/serviceAccounts"):
            assert json is not None
            account_id = json["accountId"]
            project = url.split("/projects/")[1].split("/serviceAccounts")[0]
            email = f"{account_id}@{project}.iam.gserviceaccount.com"
            self.accounts.add(email)
            self.sa_policies.setdefault(email, {"bindings": [], "etag": "e0"})
            return _FakeResponse(200, {"email": email})
        if url.endswith(":getIamPolicy"):
            email = url.split("/serviceAccounts/")[1].split(":")[0]
            policy = self.sa_policies.setdefault(email, {"bindings": [], "etag": "e0"})
            return _FakeResponse(200, policy)
        if url.endswith(":setIamPolicy"):
            assert json is not None
            email = url.split("/serviceAccounts/")[1].split(":")[0]
            policy = json["policy"]
            assert isinstance(policy, dict)
            self.sa_policies[email] = dict(policy)
            return _FakeResponse(200, policy)
        if url.endswith("/keys"):
            return _FakeResponse(200, {"privateKeyData": _FAKE_KEY_B64})
        raise AssertionError(f"unexpected POST {url}")

    def delete(self, url: str) -> _FakeResponse:
        self.calls.append(("DELETE", url))
        email = url.rsplit("/", 1)[-1]
        if email not in self.accounts:
            return _FakeResponse(404, {})
        self.accounts.discard(email)
        return _FakeResponse(200, {})


class FakeBucket:
    """A `BucketIam` backed by a real `google.api_core.iam.Policy` — see
    module docstring."""

    def __init__(self, policy: Policy | None = None) -> None:
        self.calls: list[str] = []
        # `policy or Policy(...)` would call `Policy.__len__` on a truthiness
        # check, which raises for a version-3 policy (`Policy.__check_version__`
        # forbids the dict-style access `__len__`/`__iter__` rely on) — the
        # same trap `_add_bucket_binding`/`_remove_bucket_binding` avoid by
        # using `.bindings` directly instead of `Policy`'s `Mapping` interface.
        self.policy = policy if policy is not None else Policy(version=3)

    def get_iam_policy(self, requested_policy_version: int) -> Policy:
        self.calls.append("get_iam_policy")
        return self.policy

    def set_iam_policy(self, policy: Policy) -> Policy:
        self.calls.append("set_iam_policy")
        self.policy = policy
        return policy


def _bindings_by_role(policy: Policy) -> dict[str, set[str]]:
    return {b["role"]: set(b["members"]) for b in policy.bindings}


class TestExportAccountId:
    def test_valid_stamp_produces_a_valid_account_id(self) -> None:
        assert gcp_auth.export_account_id("261108143022") == "cb-bucket-export-261108143022"

    def test_rejects_a_stamp_that_produces_a_too_long_id(self) -> None:
        with pytest.raises(ValueError, match="invalid service-account id"):
            gcp_auth.export_account_id("0123456789012345")

    def test_export_account_email(self) -> None:
        assert (
            gcp_auth.export_account_email("cb-bucket-export-s1", "my-proj")
            == "cb-bucket-export-s1@my-proj.iam.gserviceaccount.com"
        )


class TestProvision:
    """The bucket resource — not the project — gets exactly the one role
    added, and the pre-existing legacy bindings a real bucket carries (bucket
    ACL migration leaves `storage.legacyBucketOwner`/`legacyBucketReader`/
    `legacyObjectOwner`/`legacyObjectReader` behind) survive untouched."""

    _LEGACY_BINDINGS: ClassVar[list[dict[str, object]]] = [
        {"role": "roles/storage.legacyBucketOwner", "members": {"projectEditor:proj"}},
        {"role": "roles/storage.legacyBucketReader", "members": {"projectViewer:proj"}},
        {"role": "roles/storage.legacyObjectOwner", "members": {"projectEditor:proj"}},
        {"role": "roles/storage.legacyObjectReader", "members": {"projectViewer:proj"}},
    ]

    def _bucket_with_legacy_bindings(self) -> FakeBucket:
        policy = Policy(version=3)
        policy.bindings = [dict(b) for b in self._LEGACY_BINDINGS]
        return FakeBucket(policy)

    def test_grants_bucket_role_on_the_bucket_only_without_dropping_existing_bindings(
        self,
    ) -> None:
        bucket = self._bucket_with_legacy_bindings()
        http = FakeHttp()

        record = gcp_auth.provision_export_account(
            project="proj",
            bucket_name="cookiebot-bucket",
            operator_principal="user:op@example.com",
            stamp="s0000000001",
            http=http,
            bucket=bucket,
        )

        roles = _bindings_by_role(bucket.policy)
        for legacy in self._LEGACY_BINDINGS:
            assert roles[legacy["role"]] == legacy["members"], "legacy binding was touched"
        assert roles[gcp_auth.BUCKET_ROLE] == {f"serviceAccount:{record.service_account_email}"}
        assert len(roles) == len(self._LEGACY_BINDINGS) + 1
        # Called exactly once each: read the policy, write it back — no extra
        # round trips.
        assert bucket.calls == ["get_iam_policy", "set_iam_policy"]

    def test_grants_bucket_role_on_the_bucket_resource_not_the_project(self) -> None:
        """`provision_export_account` never touches a project-level IAM
        surface at all — it only ever calls `bucket.get_iam_policy`/
        `set_iam_policy`, which by construction (the `BucketIam` protocol) can
        only be a bucket resource, and it never calls any `/projects/{id}:...
        IamPolicy` IAM REST endpoint."""
        bucket = FakeBucket()
        http = FakeHttp()

        gcp_auth.provision_export_account(
            project="proj",
            bucket_name="cookiebot-bucket",
            operator_principal="user:op@example.com",
            stamp="s0000000002",
            http=http,
            bucket=bucket,
        )

        assert not any("IamPolicy" in url and "serviceAccounts" not in url for _, url in http.calls)

    def test_grants_token_creator_on_the_service_account_to_the_operator(self) -> None:
        bucket = FakeBucket()
        http = FakeHttp()

        record = gcp_auth.provision_export_account(
            project="proj",
            bucket_name="b",
            operator_principal="user:op@example.com",
            stamp="s0000000003",
            http=http,
            bucket=bucket,
        )

        policy = http.sa_policies[record.service_account_email]
        assert {"role": gcp_auth.TOKEN_CREATOR_ROLE, "members": ["user:op@example.com"]} in policy[
            "bindings"
        ]

    def test_record_names_exactly_what_was_created(self) -> None:
        bucket = FakeBucket()
        http = FakeHttp()

        record = gcp_auth.provision_export_account(
            project="proj",
            bucket_name="cookiebot-bucket",
            operator_principal="user:op@example.com",
            stamp="s0000000004",
            http=http,
            bucket=bucket,
        )

        assert (
            record.service_account_email
            == "cb-bucket-export-s0000000004@proj.iam.gserviceaccount.com"
        )
        assert record.project == "proj"
        assert record.bucket == "cookiebot-bucket"
        assert record.bucket_role == gcp_auth.BUCKET_ROLE
        assert record.token_creator_role == gcp_auth.TOKEN_CREATOR_ROLE
        assert record.operator_principal == "user:op@example.com"

    def test_granting_twice_does_not_duplicate_the_member(self) -> None:
        """Re-running `provision_export_account` for the same operator (e.g. a
        retried run) must not leave two copies of the same member on either
        policy — `_add_bucket_binding`/`_add_member` are set/list-membership
        checks, not blind appends."""
        bucket = FakeBucket()
        http = FakeHttp()

        gcp_auth.provision_export_account(
            project="proj",
            bucket_name="b",
            operator_principal="user:op@example.com",
            stamp="s0000000005",
            http=http,
            bucket=bucket,
        )
        record = gcp_auth.provision_export_account(
            project="proj",
            bucket_name="b",
            operator_principal="user:op@example.com",
            stamp="s0000000005",
            http=http,
            bucket=bucket,
        )

        roles = _bindings_by_role(bucket.policy)
        assert roles[gcp_auth.BUCKET_ROLE] == {f"serviceAccount:{record.service_account_email}"}
        sa_bindings = http.sa_policies[record.service_account_email]["bindings"]
        creator_binding = next(b for b in sa_bindings if b["role"] == gcp_auth.TOKEN_CREATOR_ROLE)
        assert creator_binding["members"].count("user:op@example.com") == 1


class TestRevoke:
    def test_removes_the_bucket_binding_and_deletes_the_account(self) -> None:
        bucket = FakeBucket()
        http = FakeHttp()
        record = gcp_auth.provision_export_account(
            project="proj",
            bucket_name="b",
            operator_principal="user:op@example.com",
            stamp="s0000000006",
            http=http,
            bucket=bucket,
        )

        result = gcp_auth.revoke_export_account(
            service_account_email=record.service_account_email,
            project="proj",
            bucket_name="b",
            http=http,
            bucket=bucket,
        )

        assert result.bucket_binding_removed is True
        assert result.service_account_deleted is True
        assert gcp_auth.BUCKET_ROLE not in _bindings_by_role(bucket.policy)

    def test_revoke_preserves_other_bindings_on_the_bucket(self) -> None:
        policy = Policy(version=3)
        policy.bindings = [
            {"role": "roles/storage.legacyBucketOwner", "members": {"projectEditor:proj"}},
            {
                "role": gcp_auth.BUCKET_ROLE,
                "members": {"serviceAccount:sa@proj.iam.gserviceaccount.com"},
            },
        ]
        bucket = FakeBucket(policy)
        http = FakeHttp()
        http.accounts.add("sa@proj.iam.gserviceaccount.com")

        gcp_auth.revoke_export_account(
            service_account_email="sa@proj.iam.gserviceaccount.com",
            project="proj",
            bucket_name="b",
            http=http,
            bucket=bucket,
        )

        roles = _bindings_by_role(bucket.policy)
        assert roles["roles/storage.legacyBucketOwner"] == {"projectEditor:proj"}
        assert gcp_auth.BUCKET_ROLE not in roles

    def test_idempotent_when_the_service_account_is_already_gone(self) -> None:
        policy = Policy(version=3)
        policy.bindings = [
            {
                "role": gcp_auth.BUCKET_ROLE,
                "members": {"serviceAccount:ghost@proj.iam.gserviceaccount.com"},
            }
        ]
        bucket = FakeBucket(policy)
        http = FakeHttp()  # account was never created in this fake

        result = gcp_auth.revoke_export_account(
            service_account_email="ghost@proj.iam.gserviceaccount.com",
            project="proj",
            bucket_name="b",
            http=http,
            bucket=bucket,
        )

        assert result.service_account_deleted is False
        assert result.bucket_binding_removed is True

    def test_idempotent_when_the_bucket_binding_is_already_gone(self) -> None:
        bucket = FakeBucket()  # empty policy, no binding
        http = FakeHttp()
        http.accounts.add("sa@proj.iam.gserviceaccount.com")

        result = gcp_auth.revoke_export_account(
            service_account_email="sa@proj.iam.gserviceaccount.com",
            project="proj",
            bucket_name="b",
            http=http,
            bucket=bucket,
        )

        assert result.bucket_binding_removed is False
        assert result.service_account_deleted is True
        # No pointless write when there was nothing to remove.
        assert bucket.calls == ["get_iam_policy"]

    def test_fully_idempotent_when_both_halves_are_already_gone(self) -> None:
        bucket = FakeBucket()
        http = FakeHttp()

        result = gcp_auth.revoke_export_account(
            service_account_email="ghost@proj.iam.gserviceaccount.com",
            project="proj",
            bucket_name="b",
            http=http,
            bucket=bucket,
        )

        assert result.bucket_binding_removed is False
        assert result.service_account_deleted is False


class TestCreateExportKey:
    def test_writes_the_key_with_mode_0600(self, tmp_path: Path) -> None:
        http = FakeHttp()
        destination = tmp_path / "key.json"

        gcp_auth.create_export_key(
            service_account_email="sa@proj.iam.gserviceaccount.com",
            project="proj",
            destination=destination,
            http=http,
        )

        assert destination.is_file()
        mode = stat.S_IMODE(destination.stat().st_mode)
        assert mode == 0o600

    def test_writes_the_decoded_key_bytes(self, tmp_path: Path) -> None:
        http = FakeHttp()
        destination = tmp_path / "sub" / "key.json"

        gcp_auth.create_export_key(
            service_account_email="sa@proj.iam.gserviceaccount.com",
            project="proj",
            destination=destination,
            http=http,
        )

        assert destination.read_bytes() == base64.b64decode(_FAKE_KEY_B64)


class TestUserCredentials:
    def test_wraps_default_credentials_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_default(scopes: list[str] | None = None) -> tuple[object, str]:
            raise DefaultCredentialsError("nope")

        monkeypatch.setattr(google.auth, "default", fake_default)

        with pytest.raises(gcp_auth.GcsAuthError, match="gcloud auth application-default login"):
            gcp_auth.user_credentials([gcp_auth.READ_ONLY_SCOPE])

    def test_passes_scopes_through_verbatim(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, object] = {}

        def fake_default(scopes: list[str] | None = None) -> tuple[object, str]:
            captured["scopes"] = scopes
            return object(), "proj"

        monkeypatch.setattr(google.auth, "default", fake_default)

        gcp_auth.user_credentials(["scope-a", "scope-b"])

        assert captured["scopes"] == ["scope-a", "scope-b"]


class TestResolveOperatorContext:
    def test_prefers_explicit_project_over_adc_project(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gcp_auth, "describe_adc", lambda: None)

        def fake_default(scopes: list[str] | None = None) -> tuple[object, str]:
            return object(), "adc-project"

        monkeypatch.setattr(google.auth, "default", fake_default)
        monkeypatch.setattr(gcp_auth, "_fetch_userinfo", lambda http: ("op@example.com", None))

        context = gcp_auth.resolve_operator_context(explicit_project="explicit-project")

        assert context.project == "explicit-project"
        assert context.project_source == "--project"

    def test_falls_back_to_adc_project_when_no_explicit_project(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gcp_auth, "describe_adc", lambda: None)

        def fake_default(scopes: list[str] | None = None) -> tuple[object, str]:
            return object(), "adc-project"

        monkeypatch.setattr(google.auth, "default", fake_default)
        monkeypatch.setattr(gcp_auth, "_fetch_userinfo", lambda http: ("op@example.com", None))

        context = gcp_auth.resolve_operator_context()

        assert context.project == "adc-project"
        assert context.project_source == "ADC"

    def test_unresolved_project_message_leads_with_project_flag_not_set_quota_project(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression guard: recommending `set-quota-project` as the primary
        fix for a missing project is actively harmful (it can 403 an
        operator's own credential on every subsequent call). `--project` must
        come first, and `set-quota-project` must not appear at all here."""
        monkeypatch.setattr(gcp_auth, "describe_adc", lambda: None)

        def fake_default(scopes: list[str] | None = None) -> tuple[object, str]:
            return object(), None

        monkeypatch.setattr(google.auth, "default", fake_default)
        monkeypatch.setattr(gcp_auth, "_fetch_userinfo", lambda http: ("op@example.com", None))

        context = gcp_auth.resolve_operator_context()

        assert context.project is None
        assert "--project" in context.project_source
        assert "set-quota-project" not in context.project_source

    def test_email_from_adc_file_needs_no_network_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        adc = gcp_auth.AdcIdentity(
            path=Path("/fake/key.json"),
            credential_type="service_account",
            email="sa@proj.iam.gserviceaccount.com",
            quota_project_id=None,
        )
        monkeypatch.setattr(gcp_auth, "describe_adc", lambda: adc)

        def fake_default(scopes: list[str] | None = None) -> tuple[object, str]:
            return object(), "proj"

        def must_not_be_called(http: object) -> tuple[str | None, str | None]:
            raise AssertionError("userinfo should not be called when the ADC file names itself")

        monkeypatch.setattr(google.auth, "default", fake_default)
        monkeypatch.setattr(gcp_auth, "_fetch_userinfo", must_not_be_called)

        context = gcp_auth.resolve_operator_context()

        assert context.email == "sa@proj.iam.gserviceaccount.com"
        assert "ADC file" in context.email_source

    def test_error_when_adc_itself_is_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gcp_auth, "describe_adc", lambda: None)

        def fake_default(scopes: list[str] | None = None) -> tuple[object, str]:
            raise DefaultCredentialsError("nope")

        monkeypatch.setattr(google.auth, "default", fake_default)

        context = gcp_auth.resolve_operator_context()

        assert context.error is not None
        assert context.credentials is None
        assert context.email is None


class TestDiagnoseGoogleError:
    def test_passes_through_unrelated_errors_unchanged(self) -> None:
        exc = RuntimeError("some other failure")
        assert gcp_auth.diagnose_google_error(exc) == str(exc)

    def test_translates_a_serviceusage_403_and_names_the_quota_project(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        adc = gcp_auth.AdcIdentity(
            path=Path("/fake/adc.json"),
            credential_type="authorized_user",
            email=None,
            quota_project_id="wrong-project",
        )
        monkeypatch.setattr(gcp_auth, "describe_adc", lambda: adc)

        exc = RuntimeError(
            "403 Caller does not have required permission to use project wrong-project. "
            "Grant the caller the roles/serviceusage.serviceUsageConsumer role"
        )

        message = gcp_auth.diagnose_google_error(exc)

        assert "wrong-project" in message
        assert "quota project" in message
        assert "serviceUsageConsumer" in message

    def test_translation_works_even_without_a_known_quota_project(self) -> None:
        exc = RuntimeError("403 forbidden: serviceusage permission missing")
        message = gcp_auth.diagnose_google_error(exc)
        assert "quota project" in message


class TestDescribeAdc:
    def test_reads_quota_project_id_when_present(self, tmp_path: Path) -> None:
        path = tmp_path / "adc.json"
        path.write_text(
            '{"type": "authorized_user", "quota_project_id": "my-quota-project"}', encoding="utf-8"
        )

        identity = gcp_auth.describe_adc(path)

        assert identity is not None
        assert identity.quota_project_id == "my-quota-project"

    def test_quota_project_id_is_none_when_absent(self, tmp_path: Path) -> None:
        path = tmp_path / "adc.json"
        path.write_text('{"type": "authorized_user"}', encoding="utf-8")

        identity = gcp_auth.describe_adc(path)

        assert identity is not None
        assert identity.quota_project_id is None

    def test_none_when_no_file_exists(self, tmp_path: Path) -> None:
        assert gcp_auth.describe_adc(tmp_path / "missing.json") is None


class TestSummarizeGoogleError:
    def test_collapses_a_wrapped_json_error_body_to_one_line(self) -> None:
        exc = RuntimeError(
            "403 POST https://iam.googleapis.com/v1/...: "
            '{"error": {"code": 403, "message": "Permission \'iam.serviceAccounts.getAccessToken\' '
            'denied on resource", "status": "PERMISSION_DENIED", "details": ['
            '{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "IAM_PERMISSION_DENIED", '
            '"domain": "iam.googleapis.com"}, '
            '{"@type": "type.googleapis.com/google.rpc.help.Link", "links": [{"description": "x", '
            '"url": "https://cloud.google.com/troubleshooter"}]}]}}'
        )

        summary = gcp_auth._summarize_google_error(exc)  # noqa: SLF001 - the function under test

        assert summary == (
            "PERMISSION_DENIED: Permission 'iam.serviceAccounts.getAccessToken' denied on resource"
        )
        # None of the wrapped-detail noise makes it into the summary.
        assert "@type" not in summary
        assert "troubleshooter" not in summary

    def test_plain_text_errors_pass_through_as_their_first_line(self) -> None:
        exc = RuntimeError("simulated: permission denied")
        assert gcp_auth._summarize_google_error(exc) == "simulated: permission denied"  # noqa: SLF001

    def test_collapses_a_refresh_error_whose_body_is_a_second_positional_arg(self) -> None:
        """`google.auth.exceptions.RefreshError` (what `impersonated_credentials
        ...refresh()` raises — see `default_impersonation_probe`) is
        constructed as `RefreshError(message, response_body)`; `str(exc)` on
        a multi-arg exception is the *Python tuple repr* of `args`, which is
        not valid JSON (its quotes are escaped as text) and must not defeat
        this function — the real body lives in `exc.args[1]` untouched.
        """
        body = json.dumps(
            {
                "error": {
                    "code": 403,
                    "message": "Permission 'iam.serviceAccounts.getAccessToken' denied on resource",
                    "status": "PERMISSION_DENIED",
                }
            },
            indent=2,
        )
        exc = google.auth.exceptions.RefreshError(
            "Unable to acquire impersonated credentials", body
        )

        summary = gcp_auth._summarize_google_error(exc)  # noqa: SLF001 - the function under test

        assert summary == (
            "PERMISSION_DENIED: Permission 'iam.serviceAccounts.getAccessToken' denied on resource"
        )
        assert "\n" not in summary
        assert "Unable to acquire" not in summary  # the wrapper message, not the JSON body


class TestVerifyImpersonation:
    def test_succeeds_immediately_when_the_probe_succeeds(self) -> None:
        calls: list[int] = []

        def probe() -> None:
            calls.append(1)

        gcp_auth.verify_impersonation(probe)

        assert len(calls) == 1

    def test_retries_and_reports_progress_until_the_probe_succeeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gcp_auth, "_IMPERSONATION_RETRY_DELAY_S", 0)
        attempts: list[int] = []
        reported: list[tuple[int, int]] = []

        def probe() -> None:
            attempts.append(1)
            if len(attempts) < 3:
                raise RuntimeError("403 iam.serviceAccounts.getAccessToken denied")

        gcp_auth.verify_impersonation(probe, on_attempt=lambda a, t: reported.append((a, t)))

        assert len(attempts) == 3
        total = gcp_auth._IMPERSONATION_RETRY_ATTEMPTS  # noqa: SLF001 - the constant under test
        assert reported == [(1, total), (2, total)]

    def test_raises_a_clear_error_after_exhausting_the_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gcp_auth, "_IMPERSONATION_RETRY_DELAY_S", 0)

        def probe() -> None:
            raise RuntimeError("403 iam.serviceAccounts.getAccessToken denied")

        with pytest.raises(gcp_auth.GcsAuthError, match="impersonating it still fails"):
            gcp_auth.verify_impersonation(probe)
