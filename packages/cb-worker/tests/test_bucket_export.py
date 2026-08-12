"""Unit tests for the cutover bucket export.

No live GCS or R2 anywhere: the destination is `store_from_uri("memory://")`
(the same in-memory backend `cb-core`'s own storage tests use — real code, not
a mock, per AGENTS.md §6's "mock the outside world only"), and the source is
`FakeBucketSource`, an in-memory stand-in for the `BucketSource` protocol —
the same split `cb_worker.importer.source` uses `DumpMongoSource` for.

`TestReadOnlyEnforcement` is the one class that talks to real
`google-cloud-storage`/`google-auth` types (`GcsReadOnlySource`, `open_source`)
rather than the fake — it exists specifically to fail the suite if a write
path or a broader-than-read-only scope is ever added back, per
`source.py`'s module docstring.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import google.auth
import pytest
from google.api_core.exceptions import GoogleAPIError
from google.auth.exceptions import DefaultCredentialsError
from google.cloud import storage as gcs_storage

from cb_core.dedupe import fingerprint
from cb_core.storage import store_from_uri
from cb_worker.bucket_export import PREFIXES, ManifestEntry, SourceBlob, gcp_auth
from cb_worker.bucket_export import manifest as manifest_io
from cb_worker.bucket_export.keys import destination_key
from cb_worker.bucket_export.runner import run_export, verify_manifest
from cb_worker.bucket_export.source import GcsReadOnlySource, GcsSourceError, open_source

_READ_ONLY_SCOPE = "https://www.googleapis.com/auth/devstorage.read_only"


@pytest.fixture(autouse=True)
def _clean_gcp_export_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every credential-path test below assumes a clean environment for the
    two variables `gcp_auth.export_credentials` branches on. A real
    `GOOGLE_APPLICATION_CREDENTIALS` or `CB_GCS_EXPORT_SERVICE_ACCOUNT`
    leaking in from the developer's own shell (or from `gcloud auth
    application-default login` having been run on this machine) must not
    silently change which credential path a test exercises.
    """
    monkeypatch.delenv(gcp_auth.KEY_FILE_ENV, raising=False)
    monkeypatch.delenv(gcp_auth.SERVICE_ACCOUNT_ENV, raising=False)


class FakeBucketSource:
    """In-memory `BucketSource` — no network, no credentials.

    `download_calls` records every name actually downloaded, which is what the
    resumability tests below assert against: a blob a prior run already landed
    must not be downloaded again.
    """

    def __init__(self, blobs: dict[str, bytes], failing: frozenset[str] = frozenset()) -> None:
        self._blobs = blobs
        self._failing = failing
        self.download_calls: list[str] = []
        self.closed = False

    def list_prefix(self, prefix: str) -> Iterator[SourceBlob]:
        for name, data in sorted(self._blobs.items()):
            if name.startswith(prefix):
                yield SourceBlob(name=name, size=len(data), updated=None, md5_hash=None)

    def download(self, name: str) -> bytes:
        self.download_calls.append(name)
        if name in self._failing:
            raise GcsSourceError(f"simulated read failure: {name}")
        return self._blobs[name]

    def close(self) -> None:
        self.closed = True


class TestPrefixInventory:
    """Guards the derived prefix set against a silent edit — see
    `bucket_export/__init__.py`'s `PREFIXES` docstring for where each one
    comes from in the v1 checkout."""

    def test_known_prefixes(self) -> None:
        assert set(PREFIXES) == {
            "IdeiaDesenho",
            "Death",
            "Countdown/BFF",
            "Countdown/Patas",
            "Countdown/FurSMeet",
            "Countdown/Furcamp",
            "Countdown/Pawstral",
            "Custom/",
            "Fight/English",
            "Fight/Portuguese",
        }


class TestKeys:
    def test_content_addressed(self) -> None:
        content_hash = fingerprint(b"hello")
        assert destination_key(content_hash, "Death/a.png") == (
            f"legacy/v1-bucket/{content_hash[:2]}/{content_hash}.png"
        )

    def test_same_bytes_same_key_regardless_of_source_path(self) -> None:
        content_hash = fingerprint(b"same bytes")
        assert destination_key(content_hash, "Death/a.png") == destination_key(
            content_hash, "Countdown/BFF/b.png"
        )

    def test_extension_taken_from_source_name(self) -> None:
        content_hash = fingerprint(b"x")
        assert destination_key(content_hash, "Custom/foo/bar.gif").endswith(".gif")

    def test_no_extension_source_yields_no_extension_key(self) -> None:
        content_hash = fingerprint(b"x")
        assert destination_key(content_hash, "Death/noext") == (
            f"legacy/v1-bucket/{content_hash[:2]}/{content_hash}"
        )

    def test_short_hash_rejected(self) -> None:
        with pytest.raises(ValueError, match="too short"):
            destination_key("ab", "Death/a.png")


class TestManifest:
    def test_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / "m.jsonl"
        entry = ManifestEntry(
            prefix="Death",
            source_path="Death/a.png",
            byte_size=3,
            content_hash="abc123",
            destination_key="legacy/v1-bucket/ab/abc123.png",
            outcome="copied",
            detail="copied",
            exported_at="2026-01-01T00:00:00+00:00",
        )
        manifest_io.append(path, entry)
        assert list(manifest_io.read_all(path)) == [entry]

    def test_latest_by_source_keeps_the_last_line(self, tmp_path: Path) -> None:
        path = tmp_path / "m.jsonl"
        first = ManifestEntry(
            prefix="Death",
            source_path="Death/a.png",
            byte_size=3,
            content_hash=None,
            destination_key=None,
            outcome="failed",
            detail="boom",
            exported_at="t1",
        )
        second = ManifestEntry(
            prefix="Death",
            source_path="Death/a.png",
            byte_size=3,
            content_hash="abc123",
            destination_key="legacy/v1-bucket/ab/abc123.png",
            outcome="copied",
            detail="copied",
            exported_at="t2",
        )
        manifest_io.append(path, first)
        manifest_io.append(path, second)
        assert manifest_io.latest_by_source(path) == {"Death/a.png": second}

    def test_missing_file_yields_nothing(self, tmp_path: Path) -> None:
        assert list(manifest_io.read_all(tmp_path / "nope.jsonl")) == []
        assert manifest_io.latest_by_source(tmp_path / "nope.jsonl") == {}

    def test_malformed_line_raises_a_clear_error(self, tmp_path: Path) -> None:
        path = tmp_path / "m.jsonl"
        path.write_text("not json\n")
        with pytest.raises(ValueError, match="malformed manifest line"):
            list(manifest_io.read_all(path))


class TestRunExport:
    async def test_dry_run_writes_nothing(self, tmp_path: Path) -> None:
        source = FakeBucketSource({"Death/a.png": b"AAA", "Death/b.png": b"BBB"})
        store = store_from_uri("memory://")
        manifest_path = tmp_path / "manifest.jsonl"

        report = await run_export(
            source, store, prefixes=("Death",), manifest_path=manifest_path, dry_run=True
        )

        assert report.total_found() == 2
        assert report.total_copied() == 2  # predicted, nothing actually written
        assert report.total_failed() == 0
        assert not manifest_path.exists()
        assert not await store.exists(destination_key(fingerprint(b"AAA"), "Death/a.png"))
        # dry-run still has to download+hash to predict correctly.
        assert set(source.download_calls) == {"Death/a.png", "Death/b.png"}

    async def test_real_run_copies_and_writes_manifest(self, tmp_path: Path) -> None:
        source = FakeBucketSource({"Death/a.png": b"AAA"})
        store = store_from_uri("memory://")
        manifest_path = tmp_path / "manifest.jsonl"

        report = await run_export(source, store, prefixes=("Death",), manifest_path=manifest_path)

        assert report.total_copied() == 1
        assert report.total_bytes() == 3
        key = destination_key(fingerprint(b"AAA"), "Death/a.png")
        assert await store.get(key) == b"AAA"

        entries = list(manifest_io.read_all(manifest_path))
        assert len(entries) == 1
        assert entries[0].outcome == "copied"
        assert entries[0].destination_key == key
        assert entries[0].source_path == "Death/a.png"

    async def test_second_run_is_idempotent_and_skips_the_download(self, tmp_path: Path) -> None:
        blobs = {"Death/a.png": b"AAA", "Death/b.png": b"BBB"}
        manifest_path = tmp_path / "manifest.jsonl"
        store = store_from_uri("memory://")

        first_source = FakeBucketSource(blobs)
        first = await run_export(
            first_source, store, prefixes=("Death",), manifest_path=manifest_path
        )
        assert first.total_copied() == 2
        assert first.total_skipped() == 0

        second_source = FakeBucketSource(blobs)
        second = await run_export(
            second_source, store, prefixes=("Death",), manifest_path=manifest_path
        )

        assert second.total_copied() == 0
        assert second.total_skipped() == 2
        # Resumed via the manifest: a blob a prior run already landed is never
        # re-downloaded, not just never re-uploaded.
        assert second_source.download_calls == []
        assert await store.get(destination_key(fingerprint(b"AAA"), "Death/a.png")) == b"AAA"

    async def test_identical_content_under_different_source_paths_dedupes(
        self, tmp_path: Path
    ) -> None:
        blobs = {"Death/a.png": b"SAME", "Countdown/BFF/b.png": b"SAME"}
        manifest_path = tmp_path / "manifest.jsonl"
        store = store_from_uri("memory://")
        source = FakeBucketSource(blobs)

        report = await run_export(
            source,
            store,
            prefixes=("Death", "Countdown/BFF"),
            manifest_path=manifest_path,
        )

        # Both are read (each one's content has to be hashed to know it's a
        # duplicate) but only one ever reaches `store().put()`.
        assert report.total_found() == 2
        assert report.total_copied() == 1
        assert report.total_skipped() == 1

    async def test_blob_failure_is_counted_not_fatal(self, tmp_path: Path) -> None:
        source = FakeBucketSource(
            {"Death/a.png": b"AAA", "Death/bad.png": b"BAD"},
            failing=frozenset({"Death/bad.png"}),
        )
        store = store_from_uri("memory://")
        manifest_path = tmp_path / "manifest.jsonl"

        report = await run_export(source, store, prefixes=("Death",), manifest_path=manifest_path)

        assert report.total_found() == 2
        assert report.total_copied() == 1
        assert report.total_failed() == 1
        assert [f.source_path for f in report.failures] == ["Death/bad.png"]

        entries = {e.source_path: e for e in manifest_io.read_all(manifest_path)}
        assert entries["Death/bad.png"].outcome == "failed"
        assert entries["Death/bad.png"].destination_key is None

    async def test_source_is_closed_by_the_caller_not_the_runner(self, tmp_path: Path) -> None:
        # run_export never closes the source itself — __main__.py owns that
        # lifecycle (finally: source.close()), same split as import-mongo's.
        source = FakeBucketSource({"Death/a.png": b"AAA"})
        store = store_from_uri("memory://")
        await run_export(source, store, prefixes=("Death",), manifest_path=tmp_path / "m.jsonl")
        assert source.closed is False


class TestVerifyManifest:
    async def test_verify_confirms_matching_objects(self, tmp_path: Path) -> None:
        source = FakeBucketSource({"Death/a.png": b"AAA"})
        store = store_from_uri("memory://")
        manifest_path = tmp_path / "manifest.jsonl"
        await run_export(source, store, prefixes=("Death",), manifest_path=manifest_path)

        report = await verify_manifest(store, manifest_path)

        assert report.checked == 1
        assert report.ok == 1
        assert report.problems == []

    async def test_verify_detects_a_missing_destination_object(self, tmp_path: Path) -> None:
        source = FakeBucketSource({"Death/a.png": b"AAA"})
        store = store_from_uri("memory://")
        manifest_path = tmp_path / "manifest.jsonl"
        await run_export(source, store, prefixes=("Death",), manifest_path=manifest_path)
        await store.delete(destination_key(fingerprint(b"AAA"), "Death/a.png"))

        report = await verify_manifest(store, manifest_path)

        assert report.ok == 0
        assert len(report.problems) == 1
        assert report.problems[0].status == "missing"

    async def test_verify_detects_hash_mismatch(self, tmp_path: Path) -> None:
        store = store_from_uri("memory://")
        manifest_path = tmp_path / "manifest.jsonl"
        key = destination_key(fingerprint(b"AAA"), "Death/a.png")
        await store.put(key, b"AAA")
        manifest_io.append(
            manifest_path,
            ManifestEntry(
                prefix="Death",
                source_path="Death/a.png",
                byte_size=3,
                content_hash="0000000000000000",  # deliberately wrong
                destination_key=key,
                outcome="copied",
                detail="copied",
                exported_at="2026-01-01T00:00:00+00:00",
            ),
        )

        report = await verify_manifest(store, manifest_path)

        assert report.problems[0].status == "hash_mismatch"

    async def test_verify_without_a_manifest_raises_a_clear_error(self, tmp_path: Path) -> None:
        store = store_from_uri("memory://")
        with pytest.raises(ValueError, match="no manifest"):
            await verify_manifest(store, tmp_path / "does-not-exist.jsonl")


class _FakeGcsBucket:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeGcsClient:
    def __init__(self, credentials: object | None = None, project: object | None = None) -> None:
        self.credentials = credentials
        self.project = project

    def bucket(self, name: str) -> _FakeGcsBucket:
        return _FakeGcsBucket(name)


class _BrokenGcsBucket(_FakeGcsBucket):
    """A bucket handle whose listing and blob calls always fail, to exercise
    the error-wrapping path in `GcsReadOnlySource.list_prefix`/`.download`."""

    def list_blobs(self, prefix: str) -> list[object]:
        raise GoogleAPIError("boom")

    def blob(self, name: str) -> object:
        raise GoogleAPIError("boom")


class _BrokenGcsClient(_FakeGcsClient):
    def bucket(self, name: str) -> _BrokenGcsBucket:
        return _BrokenGcsBucket(name)


class TestReadOnlyEnforcement:
    """The hard requirement: the source bucket must never be written to.

    These tests fail the build, not just review, if `GcsReadOnlySource` ever
    grows a write-capable method or `open_source` ever stops asking for the
    read-only scope specifically.
    """

    def test_source_exposes_exactly_list_and_download(self) -> None:
        public_methods = {
            name
            for name in dir(GcsReadOnlySource)
            if not name.startswith("_") and callable(getattr(GcsReadOnlySource, name))
        }
        assert public_methods == {"list_prefix", "download", "close"}

    def test_no_public_method_name_looks_write_capable(self) -> None:
        write_verbs = (
            "delete",
            "upload",
            "patch",
            "update",
            "insert",
            "copy",
            "rewrite",
            "compose",
            "create",
            "make_public",
            "set_iam",
            "acl",
        )
        public_methods = {
            name
            for name in dir(GcsReadOnlySource)
            if not name.startswith("_") and callable(getattr(GcsReadOnlySource, name))
        }
        for name in public_methods:
            lowered = name.lower()
            for verb in write_verbs:
                assert verb not in lowered, f"{name!r} looks write-capable"

    def test_the_underlying_bucket_handle_never_leaks(self) -> None:
        source = GcsReadOnlySource(_FakeGcsClient(), "cookiebot-bucket")
        assert not hasattr(source, "bucket")
        # The only handle is the private attribute; nothing public returns it.
        for name in ("list_prefix", "download", "close"):
            assert "bucket" not in name

    def test_open_source_requests_exactly_the_read_only_scope(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, object] = {}

        def fake_default(scopes: list[str] | None = None) -> tuple[object, str]:
            captured["scopes"] = scopes
            return object(), "fake-project"

        monkeypatch.setattr(google.auth, "default", fake_default)
        monkeypatch.setattr(gcs_storage, "Client", _FakeGcsClient)

        open_source("cookiebot-bucket")

        assert captured["scopes"] == [_READ_ONLY_SCOPE]

    def test_open_source_rejects_empty_bucket_name(self) -> None:
        with pytest.raises(ValueError, match="no source bucket configured"):
            open_source("")

    def test_open_source_reports_missing_credentials_clearly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_default(scopes: list[str] | None = None) -> tuple[object, str]:
            raise DefaultCredentialsError("no credentials here")

        monkeypatch.setattr(google.auth, "default", fake_default)

        with pytest.raises(GcsSourceError, match="GOOGLE_APPLICATION_CREDENTIALS"):
            open_source("cookiebot-bucket")

    def test_open_source_error_names_gcs_auth_provision(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The way out of a missing credential is no longer only "set an env
        var" — it is also `gcs-auth provision`, and the error message says so."""

        def fake_default(scopes: list[str] | None = None) -> tuple[object, str]:
            raise DefaultCredentialsError("no credentials here")

        monkeypatch.setattr(google.auth, "default", fake_default)

        with pytest.raises(GcsSourceError, match="gcs-auth provision"):
            open_source("cookiebot-bucket")

    def test_list_prefix_wraps_backend_errors(self) -> None:
        source = GcsReadOnlySource(_BrokenGcsClient(), "cookiebot-bucket")

        with pytest.raises(GcsSourceError, match="listing"):
            list(source.list_prefix("Death"))

    def test_download_wraps_backend_errors(self) -> None:
        source = GcsReadOnlySource(_BrokenGcsClient(), "cookiebot-bucket")

        with pytest.raises(GcsSourceError, match="downloading"):
            source.download("Death/a.png")

    # ---- the read-only scope survives every credential path gcp_auth offers ----
    # (extends the enforcement above, which only exercised `open_source`'s own
    # default-ADC path; these three prove the same guarantee for the other two
    # paths `export_credentials` can take, per its own module docstring.)

    def test_export_credentials_adc_path_is_read_only_scope(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, object] = {}

        def fake_default(scopes: list[str] | None = None) -> tuple[object, str | None]:
            captured["scopes"] = scopes
            return object(), "fake-project"

        monkeypatch.setattr(google.auth, "default", fake_default)

        gcp_auth.export_credentials()

        assert captured["scopes"] == [gcp_auth.READ_ONLY_SCOPE]

    def test_export_credentials_key_file_path_is_read_only_scope(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        captured: dict[str, object] = {}

        class _FakeServiceAccountCredentials:
            def __init__(self, scopes: object) -> None:
                self.scopes = scopes
                self.project_id = "fake-project"

        def fake_from_service_account_file(filename: str, scopes: object = None) -> object:
            captured["filename"] = filename
            captured["scopes"] = scopes
            return _FakeServiceAccountCredentials(scopes)

        monkeypatch.setattr(
            gcp_auth.service_account.Credentials,
            "from_service_account_file",
            fake_from_service_account_file,
        )

        key_path = tmp_path / "key.json"
        credentials, _ = gcp_auth.export_credentials(key_file=str(key_path))

        assert captured["scopes"] == [gcp_auth.READ_ONLY_SCOPE]
        assert credentials.scopes == [gcp_auth.READ_ONLY_SCOPE]  # type: ignore[attr-defined]

    def test_export_credentials_impersonation_path_is_read_only_scope(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, object] = {}

        def fake_default(scopes: list[str] | None = None) -> tuple[object, str | None]:
            return object(), "fake-project"

        class _FakeImpersonatedCredentials:
            def __init__(
                self,
                *,
                source_credentials: object,
                target_principal: str,
                target_scopes: list[str],
                lifetime: int,
            ) -> None:
                captured["target_principal"] = target_principal
                captured["target_scopes"] = target_scopes
                self.service_account_email = target_principal

        monkeypatch.setattr(google.auth, "default", fake_default)
        monkeypatch.setattr(
            gcp_auth.impersonated_credentials, "Credentials", _FakeImpersonatedCredentials
        )
        monkeypatch.setenv(gcp_auth.SERVICE_ACCOUNT_ENV, "export-sa@proj.iam.gserviceaccount.com")

        gcp_auth.export_credentials()

        assert captured["target_scopes"] == [gcp_auth.READ_ONLY_SCOPE]
        assert captured["target_principal"] == "export-sa@proj.iam.gserviceaccount.com"
