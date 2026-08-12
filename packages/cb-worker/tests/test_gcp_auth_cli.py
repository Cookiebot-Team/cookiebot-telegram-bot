"""Unit tests for `cb_worker.bucket_export.gcp_auth_cli` — no real Google IAM,
storage or network anywhere. `gcp_auth.resolve_operator_context`,
`default_http_client` and `default_bucket_iam` are monkeypatched at the exact
seams `gcp_auth.py` itself defines for this purpose; `provision_export_account`/
`revoke_export_account` run for real against the in-memory `FakeHttp`/
`FakeBucket` those seams then hand back, so the CLI-to-business-logic wiring
is actually exercised, not just mocked away.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from google.api_core.iam import Policy

from cb_worker.bucket_export import gcp_auth
from cb_worker.bucket_export import gcp_auth_cli as cli

_FAKE_KEY_B64 = base64.b64encode(b'{"type": "service_account", "project_id": "proj"}').decode()

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\x1b\][^\x07]*\x07|\x1b[()][A-Za-z0-9]")


def _plain(text: str) -> str:
    """Strip `rich`'s ANSI/cursor-control sequences so an assertion can look
    for a substring without caring how the table around it was styled — a
    literal like `CB_GCS_EXPORT_SERVICE_ACCOUNT=` can otherwise land with a
    color-reset escape sequence spliced in the middle of it."""
    return _ANSI_ESCAPE.sub("", text)


class _FakeProgress:
    """A no-op stand-in for `rich.progress.Progress`."""

    def __enter__(self) -> _FakeProgress:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def add_task(self, *args: object, **kwargs: object) -> int:
        return 0

    def update(self, *args: object, **kwargs: object) -> None:
        return None


@pytest.fixture(autouse=True)
def _no_rich_progress_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    """`rich.progress.Progress` (via `Live`) can run a background
    auto-refresh thread that keeps writing to `console.file` after its own
    `with` block has exited — under `capsys`, whose captured stream is closed
    between tests, a leftover write from an *earlier* test's thread surfaces
    as `ValueError: I/O operation on closed file` in whichever *later* test
    happens to be running when it lands, which is exactly as confusing as it
    sounds. Replacing `Progress` with a no-op here tests this module's own
    wiring — does it call `provision_export_account` with the right
    arguments, does it report the right exit code — without depending on
    `rich`'s live-render thread behaving any particular way under a fake
    terminal.
    """
    monkeypatch.setattr(cli, "Progress", lambda *args, **kwargs: _FakeProgress())


@pytest.fixture(autouse=True)
def _no_global_logging_reconfiguration(monkeypatch: pytest.MonkeyPatch) -> None:
    """`cli.main` calls `configure_logging(settings)` on every invocation,
    the same as every other `__main__.py` in this package — a legitimate
    thing for a real process entry point to do exactly once, but `structlog`
    configuration is process-global, not per-call: it binds its output to
    whatever `sys.stdout` *is* at the moment `configure_logging` runs, not a
    live lookup. Calling it from inside a test therefore repoints every
    `structlog` logger in the whole process at *this test's* `capsys` buffer,
    which is closed the moment the test ends — and the next test anywhere in
    the session that logs a line (this file or any other) then crashes with
    `ValueError: I/O operation on closed file`, having done nothing wrong
    itself. Standing this up as a no-op here is what keeps this test file's
    use of the real CLI entry point from corrupting global state for every
    other test in the run.
    """
    monkeypatch.setattr(cli, "configure_logging", lambda settings: None)


@pytest.fixture(autouse=True)
def _impersonation_already_verified(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_cmd_provision` confirms the tokenCreator grant actually works
    (`gcp_auth.verify_impersonation`, calling `default_impersonation_probe`)
    before declaring success — a real network call against a real project
    none of these tests have. Defaulting it to "already verified" here keeps
    every test that is not specifically about this behaviour from paying for
    it (or hanging on a fake `object()` credential); the two tests that do
    care about it override this with their own `monkeypatch.setattr` call.
    """
    monkeypatch.setattr(gcp_auth, "verify_impersonation", lambda attempt, **kwargs: None)


class _FakeResponse:
    def __init__(self, status_code: int, body: Mapping[str, object]) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> Mapping[str, object]:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeHttp:
    """Same shape as `test_gcp_auth.py`'s own fake, duplicated rather than
    imported: each test file in this suite owns its fixtures, and this one is
    small enough that sharing it would cost more in coupling than it saves."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.sa_policies: dict[str, dict[str, Any]] = {}
        self.accounts: set[str] = set()

    def get(self, url: str, *, params: Mapping[str, str] | None = None) -> _FakeResponse:
        # `:getIamPolicy` is POST-only on `iam.googleapis.com` (see
        # `gcp_auth._get_service_account_policy`'s own docstring) — this fake
        # answers no `GET` at all, on purpose, so a regression back to that
        # verb fails here instead of only against a live project.
        self.calls.append(("GET", url))
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
    def __init__(self, policy: Policy | None = None) -> None:
        self.calls: list[str] = []
        self.policy = policy if policy is not None else Policy(version=3)

    def get_iam_policy(self, requested_policy_version: int) -> Policy:
        self.calls.append("get_iam_policy")
        return self.policy

    def set_iam_policy(self, policy: Policy) -> Policy:
        self.calls.append("set_iam_policy")
        self.policy = policy
        return policy


def _ok_context(**overrides: object) -> gcp_auth.OperatorContext:
    defaults: dict[str, object] = {
        "email": "op@example.com",
        "email_source": "userinfo endpoint",
        "project": "proj",
        "project_source": "ADC",
        "credentials": object(),
        "error": None,
    }
    defaults.update(overrides)
    return gcp_auth.OperatorContext(**defaults)  # type: ignore[arg-type]


def _must_not_be_called(*args: object, **kwargs: object) -> object:
    raise AssertionError(f"unexpected call with args={args!r} kwargs={kwargs!r}")


class TestStatusExitCode:
    def test_clean_report_with_no_credentials_exits_0(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gcp_auth, "describe_adc", lambda: None)
        monkeypatch.setattr(
            gcp_auth,
            "resolve_operator_context",
            lambda **_: _ok_context(
                email=None,
                email_source="not resolved",
                project=None,
                project_source="unset",
                credentials=None,
                error="no Google credentials found. Run `gcloud auth application-default login`.",
            ),
        )

        assert cli.main(["status"]) == 0

    def test_bucket_unconfigured_is_a_skip_not_a_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gcp_auth, "describe_adc", lambda: None)
        monkeypatch.setattr(gcp_auth, "resolve_operator_context", lambda **_: _ok_context())

        rows = cli.collect_status("")

        assert any(r.check == "bucket listable" and "skip" in r.value for r in rows)


class TestProvisionUserErrors:
    def test_missing_bucket_flag_exits_2(self) -> None:
        with pytest.raises(SystemExit) as excinfo:
            cli.main(["provision"])
        assert excinfo.value.code == 2

    def test_no_operator_and_none_resolvable_exits_2(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(
            gcp_auth,
            "resolve_operator_context",
            lambda **_: _ok_context(email=None, email_source="unresolved"),
        )

        code = cli.main(["provision", "--bucket", "cookiebot-bucket", "--yes"])

        assert code == 2
        assert "operator" in capsys.readouterr().err

    def test_no_project_resolvable_exits_2(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(
            gcp_auth,
            "resolve_operator_context",
            lambda **_: _ok_context(project=None, project_source="unset"),
        )

        code = cli.main(["provision", "--bucket", "cookiebot-bucket", "--yes"])

        assert code == 2
        assert "project" in capsys.readouterr().err


class TestProvisionDryRun:
    def test_makes_zero_mutating_calls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The whole point of `--dry-run`: none of the functions that could
        possibly write anything are even reachable, so monkeypatching every
        one of them to blow up if called is a stronger proof than counting
        calls on a fake afterward — there is no path left that could dodge
        the assertion."""
        monkeypatch.setattr(gcp_auth, "resolve_operator_context", lambda **_: _ok_context())
        monkeypatch.setattr(gcp_auth, "default_http_client", _must_not_be_called)
        monkeypatch.setattr(gcp_auth, "default_bucket_iam", _must_not_be_called)
        monkeypatch.setattr(gcp_auth, "provision_export_account", _must_not_be_called)
        monkeypatch.setattr(gcp_auth, "create_export_key", _must_not_be_called)

        code = cli.main(["provision", "--bucket", "cookiebot-bucket", "--dry-run"])

        assert code == 0

    def test_dry_run_works_with_nothing_resolved_at_all(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """This is the machine-with-no-credentials-at-all case: the plan still
        prints (with placeholders) and the command still exits 0."""
        monkeypatch.setattr(
            gcp_auth,
            "resolve_operator_context",
            lambda **_: _ok_context(
                email=None,
                email_source="unresolved",
                project=None,
                project_source="unset",
                credentials=None,
                error="no Google credentials found",
            ),
        )
        monkeypatch.setattr(gcp_auth, "default_http_client", _must_not_be_called)
        monkeypatch.setattr(gcp_auth, "default_bucket_iam", _must_not_be_called)
        monkeypatch.setattr(gcp_auth, "provision_export_account", _must_not_be_called)

        code = cli.main(["provision", "--bucket", "cookiebot-bucket", "--dry-run"])

        assert code == 0


class TestProvisionConfirmation:
    def test_declining_confirmation_exits_2_and_provisions_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gcp_auth, "resolve_operator_context", lambda **_: _ok_context())
        monkeypatch.setattr(gcp_auth, "default_http_client", _must_not_be_called)
        monkeypatch.setattr(gcp_auth, "default_bucket_iam", _must_not_be_called)
        monkeypatch.setattr(gcp_auth, "provision_export_account", _must_not_be_called)
        monkeypatch.setattr("rich.prompt.Confirm.ask", lambda *a, **k: False)

        code = cli.main(["provision", "--bucket", "cookiebot-bucket"])

        assert code == 2


class TestProvisionSuccess:
    def test_real_run_provisions_and_prints_the_export_line(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        fake_http = FakeHttp()
        fake_bucket = FakeBucket()

        monkeypatch.setattr(gcp_auth, "resolve_operator_context", lambda **_: _ok_context())
        monkeypatch.setattr(gcp_auth, "default_http_client", lambda credentials: fake_http)
        monkeypatch.setattr(
            gcp_auth, "default_bucket_iam", lambda bucket, credentials, project: fake_bucket
        )

        code = cli.main(["provision", "--bucket", "cookiebot-bucket", "--yes"])

        assert code == 0
        out = capsys.readouterr().out
        assert f"{gcp_auth.SERVICE_ACCOUNT_ENV}=" in _plain(out)
        assert gcp_auth.BUCKET_ROLE in {b["role"] for b in fake_bucket.policy.bindings}


class TestProvisionImpersonationVerification:
    """`_cmd_provision` waits for the tokenCreator grant to actually take
    effect before declaring success — see `gcp_auth.verify_impersonation`'s
    own docstring for why the grant API call returning 200 is not enough."""

    def test_verification_failure_still_exits_1_but_reports_the_account(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        fake_http = FakeHttp()
        fake_bucket = FakeBucket()

        monkeypatch.setattr(gcp_auth, "resolve_operator_context", lambda **_: _ok_context())
        monkeypatch.setattr(gcp_auth, "default_http_client", lambda credentials: fake_http)
        monkeypatch.setattr(
            gcp_auth, "default_bucket_iam", lambda bucket, credentials, project: fake_bucket
        )

        def _never_verified(attempt: object, **kwargs: object) -> None:
            raise gcp_auth.GcsAuthError("impersonation still fails after 6 attempts: 403 denied")

        monkeypatch.setattr(gcp_auth, "verify_impersonation", _never_verified)

        code = cli.main(["provision", "--bucket", "cookiebot-bucket", "--yes"])

        assert code == 1
        out, err = capsys.readouterr()
        # The account really was created — the CLI still reports it, plus a
        # warning, rather than hiding a partial (but real) success behind a
        # bare non-zero exit code.
        assert f"{gcp_auth.SERVICE_ACCOUNT_ENV}=" in _plain(out)
        assert "gcs-auth status" in err

    def test_verification_success_is_a_clean_exit_0(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        fake_http = FakeHttp()
        fake_bucket = FakeBucket()
        calls: list[object] = []

        monkeypatch.setattr(gcp_auth, "resolve_operator_context", lambda **_: _ok_context())
        monkeypatch.setattr(gcp_auth, "default_http_client", lambda credentials: fake_http)
        monkeypatch.setattr(
            gcp_auth, "default_bucket_iam", lambda bucket, credentials, project: fake_bucket
        )

        def _verified_on_first_try(attempt: object, **kwargs: object) -> None:
            calls.append(attempt)

        monkeypatch.setattr(gcp_auth, "verify_impersonation", _verified_on_first_try)

        code = cli.main(["provision", "--bucket", "cookiebot-bucket", "--yes"])

        assert code == 0
        assert len(calls) == 1
        assert "warning" not in capsys.readouterr().err.lower()


class TestProvisionFailure:
    def test_a_failed_provisioning_call_exits_1(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(gcp_auth, "resolve_operator_context", lambda **_: _ok_context())
        monkeypatch.setattr(gcp_auth, "default_http_client", lambda credentials: FakeHttp())
        monkeypatch.setattr(
            gcp_auth, "default_bucket_iam", lambda bucket, credentials, project: FakeBucket()
        )

        def boom(**kwargs: object) -> object:
            raise RuntimeError("iam is down")

        monkeypatch.setattr(gcp_auth, "provision_export_account", boom)

        code = cli.main(["provision", "--bucket", "cookiebot-bucket", "--yes"])

        assert code == 1
        assert "iam is down" in capsys.readouterr().err


class TestRevokeUserErrors:
    def test_missing_required_flags_exits_2(self) -> None:
        with pytest.raises(SystemExit) as excinfo:
            cli.main(["revoke"])
        assert excinfo.value.code == 2


class TestRevokeIdempotency:
    def test_revoking_an_already_gone_account_still_exits_0_and_says_so(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        fake_bucket = FakeBucket()  # empty policy: no binding to remove
        fake_http = FakeHttp()  # no accounts created: nothing to delete

        monkeypatch.setattr(gcp_auth, "resolve_operator_context", lambda **_: _ok_context())
        monkeypatch.setattr(gcp_auth, "default_http_client", lambda credentials: fake_http)
        monkeypatch.setattr(
            gcp_auth, "default_bucket_iam", lambda bucket, credentials, project: fake_bucket
        )

        code = cli.main(
            [
                "revoke",
                "--service-account",
                "ghost@proj.iam.gserviceaccount.com",
                "--bucket",
                "cookiebot-bucket",
                "--yes",
            ]
        )

        assert code == 0
        out = capsys.readouterr().out
        assert "already" in out.lower()


class TestRevokeFailure:
    def test_a_failed_revoke_call_exits_1(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(gcp_auth, "resolve_operator_context", lambda **_: _ok_context())
        monkeypatch.setattr(gcp_auth, "default_http_client", lambda credentials: FakeHttp())
        monkeypatch.setattr(
            gcp_auth, "default_bucket_iam", lambda bucket, credentials, project: FakeBucket()
        )

        def boom(**kwargs: object) -> object:
            raise RuntimeError("iam is down")

        monkeypatch.setattr(gcp_auth, "revoke_export_account", boom)

        code = cli.main(
            [
                "revoke",
                "--service-account",
                "sa@proj.iam.gserviceaccount.com",
                "--bucket",
                "cookiebot-bucket",
                "--yes",
            ]
        )

        assert code == 1
        assert "iam is down" in capsys.readouterr().err


class TestKeyFileCreation:
    def test_provision_with_key_file_writes_it_and_warns(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        fake_http = FakeHttp()
        fake_bucket = FakeBucket()
        key_path = tmp_path / "key.json"

        monkeypatch.setattr(gcp_auth, "resolve_operator_context", lambda **_: _ok_context())
        monkeypatch.setattr(gcp_auth, "default_http_client", lambda credentials: fake_http)
        monkeypatch.setattr(
            gcp_auth, "default_bucket_iam", lambda bucket, credentials, project: fake_bucket
        )

        code = cli.main(
            [
                "provision",
                "--bucket",
                "cookiebot-bucket",
                "--yes",
                "--key-file",
                str(key_path),
            ]
        )

        assert code == 0
        assert key_path.is_file()
        import stat as stat_module

        assert stat_module.S_IMODE(key_path.stat().st_mode) == 0o600
        out = capsys.readouterr().out
        assert "warning" in out.lower()
