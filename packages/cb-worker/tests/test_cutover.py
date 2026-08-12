"""Unit tests for `cb_worker.cutover` — step selection, dry-run, and the
"one step's failure never costs the rest" contract.

No real Postgres, Mongo or network anywhere here. `mongo` uses a real
`mongodump`-shaped BSON directory (`DumpMongoSource` just reads local files,
same as `test_importer_source.py`); `bucket`'s GCS source is monkeypatched to
an in-memory fake (same shape as `FakeBucketSource` in `test_bucket_export.py`);
object storage is either the real in-memory backend or a real `file://`
directory under `tmp_path` — both are `cb_core.storage`'s own code, not a
mock, per AGENTS.md §6's "mock the outside world only". Wherever a real
Postgres pool would otherwise be attempted, the test either selects steps that
never need one, or proves the pool is never touched at all by monkeypatching
`db.init_pool` to fail the test if called.
"""

from __future__ import annotations

import io
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any

import bson
import pytest
from PIL import Image
from rich.console import Console

import cb_worker.cutover.__main__ as cutover_main
from cb_core.meme_templates import MemeTemplate, all_templates
from cb_core.settings import Settings
from cb_core.storage import store_from_uri
from cb_worker import meme_seed
from cb_worker.bucket_export import SourceBlob
from cb_worker.bucket_export.source import GcsSourceError
from cb_worker.cutover import STEP_ORDER, StepSelectionError, resolve_steps
from cb_worker.cutover import runner as cutover_runner


def _console() -> Console:
    # A real `Console` writing to a throwaway buffer: exercising the actual
    # rendering path (`render_preflight`/`render_summary`/`render_verify`
    # against a real `rich.table.Table`) without printing ANSI noise into the
    # test run.
    return Console(file=io.StringIO(), no_color=True, width=120)


def _write_bson(path: Path, docs: Iterable[Mapping[str, Any]]) -> None:
    path.write_bytes(b"".join(bson.encode(doc) for doc in docs))


def _fake_v1_checkout(root: Path, template: MemeTemplate) -> None:
    """One catalog entry, present on disk — same shape `test_meme_job.py`'s own
    helper builds, duplicated here for the same reason every other test file
    in this suite duplicates its own small fixtures rather than sharing a
    conftest."""
    directory = root / meme_seed.V1_SUBPATH / template.language
    directory.mkdir(parents=True, exist_ok=True)
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), "white").save(buffer, format="PNG")
    (directory / template.filename).write_bytes(buffer.getvalue())


def _single_template_catalog(monkeypatch: pytest.MonkeyPatch) -> MemeTemplate:
    """Shrinks the meme catalog to one real entry, so `meme_seed.seed()` can
    report a clean `ok` without all 801 real template files on disk — every
    test below cares whether the *memes step* ran and what it wrote, not
    whether this fixture happens to carry the full v1 catalog.

    `all_templates()` is patched in both modules that call it
    (`cb_worker.meme_seed`, `cb_worker.cutover.runner`): each bound the name in
    its own module namespace at import time, so patching the origin function
    in `cb_core.meme_templates` alone would not reach either call site.
    """
    template = all_templates()[0]
    monkeypatch.setattr(meme_seed, "all_templates", lambda: (template,))
    monkeypatch.setattr(cutover_runner, "all_templates", lambda: (template,))
    return template


class FakeBucketSource:
    """In-memory `BucketSource`, no Google credentials or network — the same
    role `FakeBucketSource` plays in `test_bucket_export.py`."""

    def __init__(self, blobs: dict[str, bytes]) -> None:
        self._blobs = blobs
        self.closed = False

    def list_prefix(self, prefix: str) -> Iterator[SourceBlob]:
        for name, data in sorted(self._blobs.items()):
            if name.startswith(prefix):
                yield SourceBlob(name=name, size=len(data), updated=None, md5_hash=None)

    def download(self, name: str) -> bytes:
        return self._blobs[name]

    def close(self) -> None:
        self.closed = True


class TestResolveSteps:
    def test_default_is_every_step_in_order(self) -> None:
        assert resolve_steps() == STEP_ORDER

    def test_only_narrows_and_keeps_step_order_regardless_of_typed_order(self) -> None:
        assert resolve_steps(only="memes,mongo") == ("mongo", "memes")

    def test_skip_removes_from_the_default_full_set(self) -> None:
        assert resolve_steps(skip="schema,bucket") == tuple(
            s for s in STEP_ORDER if s not in ("schema", "bucket")
        )

    def test_only_and_skip_compose(self) -> None:
        assert resolve_steps(only="mongo,bucket,memes", skip="bucket") == ("mongo", "memes")

    def test_unknown_only_name_raises_naming_the_bad_token(self) -> None:
        with pytest.raises(StepSelectionError, match="bucet"):
            resolve_steps(only="mongo,bucet")

    def test_unknown_skip_name_raises(self) -> None:
        with pytest.raises(StepSelectionError, match="verifyy"):
            resolve_steps(skip="verifyy")


class TestCLIStepSelection:
    def test_unknown_step_name_exits_2(self, capsys: pytest.CaptureFixture[str]) -> None:
        code = cutover_main.main(["--only", "bogus"])

        assert code == 2
        assert "bogus" in capsys.readouterr().err

    def test_only_mongo_without_a_configured_source_exits_2(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(
            cutover_main, "get_settings", lambda: Settings(mongo_uri="", mongo_dump_dir="")
        )

        code = cutover_main.main(["--only", "mongo"])

        assert code == 2
        assert "CB_MONGO_URI" in capsys.readouterr().err

    def test_only_bucket_without_dest_uri_exits_2(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.delenv("CB_BUCKET_EXPORT_DEST_URI", raising=False)
        monkeypatch.delenv("CB_BUCKET_EXPORT_SOURCE_BUCKET", raising=False)
        monkeypatch.setattr(cutover_main, "get_settings", Settings)

        code = cutover_main.main(["--only", "bucket"])

        assert code == 2
        assert "CB_BUCKET_EXPORT_DEST_URI" in capsys.readouterr().err

    def test_no_steps_selected_is_a_clean_no_op(self) -> None:
        # --only and --skip naming the same step is not an error — there is
        # simply nothing to run.
        assert cutover_main.main(["--only", "verify", "--skip", "verify"]) == 0


class TestDryRunWritesNothing:
    async def test_mongo_dry_run_never_opens_the_db_pool(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        dump_dir = tmp_path / "dump"
        dump_dir.mkdir()
        _write_bson(
            dump_dir / "groups.bson",
            [{"groupId": "1", "name": "G", "imageUrl": None, "adminUsers": []}],
        )
        settings = Settings(mongo_uri="", mongo_dump_dir=str(dump_dir))

        async def _must_not_be_called(*args: object, **kwargs: object) -> None:
            raise AssertionError("db.init_pool must not run for a dry-run mongo-only selection")

        monkeypatch.setattr(cutover_runner.db, "init_pool", _must_not_be_called)

        report = await cutover_runner.run_cutover(
            settings,
            ("mongo",),
            dry_run=True,
            collections=None,
            memes_source=tmp_path,
            bucket_manifest_path=tmp_path / "bucket_manifest.jsonl",
            console=_console(),
        )

        assert [s.status for s in report.steps] == ["ok"]
        assert "1 row" in report.steps[0].headline

    async def test_bucket_and_memes_dry_run_write_nothing_to_a_real_store(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A real `file://` store, not `memory://`: a fresh instance re-opened
        # after the run still sees whatever was actually written to disk,
        # which is what lets this test prove a negative.
        store_dir = tmp_path / "store"
        checkout = tmp_path / "checkout"
        template = _single_template_catalog(monkeypatch)
        _fake_v1_checkout(checkout, template)
        manifest_path = tmp_path / "bucket_manifest.jsonl"

        monkeypatch.setenv("CB_BUCKET_EXPORT_DEST_URI", f"file://{store_dir}")
        monkeypatch.setenv("CB_BUCKET_EXPORT_SOURCE_BUCKET", "v1-legacy-bucket")
        monkeypatch.setattr(
            cutover_runner,
            "open_bucket_source",
            lambda bucket_name: FakeBucketSource({"Death/a.png": b"AAA"}),
        )

        settings = Settings(storage_uri=f"file://{store_dir}")

        report = await cutover_runner.run_cutover(
            settings,
            ("bucket", "memes"),
            dry_run=True,
            collections=None,
            memes_source=checkout,
            bucket_manifest_path=manifest_path,
            console=_console(),
        )

        assert {s.step: s.status for s in report.steps} == {"bucket": "ok", "memes": "ok"}
        # `run_export` only skips `store().put()` and the manifest append under
        # `dry_run` (its own module docstring) — the manifest file itself is
        # the cheapest, most direct proof nothing was written.
        assert not manifest_path.exists()

        reopened = store_from_uri(f"file://{store_dir}")
        try:
            assert not await reopened.exists(template.storage_key)
        finally:
            await reopened.close()


class TestFailingStepDoesNotAbortOthers:
    async def test_bucket_failure_does_not_stop_memes_and_marks_the_run_failed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        store_dir = tmp_path / "store"
        checkout = tmp_path / "checkout"
        template = _single_template_catalog(monkeypatch)
        _fake_v1_checkout(checkout, template)

        monkeypatch.setenv("CB_BUCKET_EXPORT_DEST_URI", f"file://{store_dir}")
        monkeypatch.setenv("CB_BUCKET_EXPORT_SOURCE_BUCKET", "v1-legacy-bucket")

        def _raise(bucket_name: str) -> FakeBucketSource:
            raise GcsSourceError("simulated: no Google credentials in this environment")

        monkeypatch.setattr(cutover_runner, "open_bucket_source", _raise)
        settings = Settings(storage_uri=f"file://{store_dir}")

        report = await cutover_runner.run_cutover(
            settings,
            ("bucket", "memes"),
            dry_run=False,
            collections=None,
            memes_source=checkout,
            bucket_manifest_path=tmp_path / "bucket_manifest.jsonl",
            console=_console(),
        )

        statuses = {s.step: s.status for s in report.steps}
        assert statuses["bucket"] == "failed"
        assert statuses["memes"] == "ok"  # never aborted by the bucket step blowing up
        assert report.any_failed() is True

    def test_cli_exit_code_is_1_when_any_step_fails(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        store_dir = tmp_path / "store"
        checkout = tmp_path / "checkout"
        template = _single_template_catalog(monkeypatch)
        _fake_v1_checkout(checkout, template)

        monkeypatch.setenv("CB_BUCKET_EXPORT_DEST_URI", f"file://{store_dir}")
        monkeypatch.setenv("CB_BUCKET_EXPORT_SOURCE_BUCKET", "v1-legacy-bucket")

        def _raise(bucket_name: str) -> FakeBucketSource:
            raise GcsSourceError("simulated: no Google credentials in this environment")

        monkeypatch.setattr(cutover_runner, "open_bucket_source", _raise)
        settings = Settings(storage_uri=f"file://{store_dir}")
        monkeypatch.setattr(cutover_main, "get_settings", lambda: settings)

        code = cutover_main.main(["--only", "bucket,memes", "--source", str(checkout), "--yes"])

        assert code == 1


class TestGcsExportPreflight:
    """`_check_gcs_export` — the row `run_preflight` gained alongside the GCS
    provisioning tooling. No real Google credentials or network: the
    credential lookup and the listability probe are each monkeypatched at
    the exact seam `cutover_runner` calls through (`gcp_auth.export_credentials`,
    `open_bucket_source`), the same style every other preflight check in this
    file already uses for its own dependency.
    """

    def test_skip_when_no_bucket_configured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CB_BUCKET_EXPORT_SOURCE_BUCKET", raising=False)

        check = cutover_runner._check_gcs_export()  # noqa: SLF001 - the check under test

        assert check.status == "skip"
        assert "CB_BUCKET_EXPORT_SOURCE_BUCKET" in check.detail

    def test_ok_when_credentials_resolve_and_the_bucket_lists(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CB_BUCKET_EXPORT_SOURCE_BUCKET", "v1-legacy-bucket")
        monkeypatch.setattr(
            cutover_runner.gcp_auth, "export_credentials", lambda: (object(), "fake-project")
        )
        monkeypatch.setattr(
            cutover_runner,
            "open_bucket_source",
            lambda bucket_name: FakeBucketSource({"Death/a.png": b"AAA"}),
        )

        check = cutover_runner._check_gcs_export()  # noqa: SLF001 - the check under test

        assert check.status == "ok"
        assert "v1-legacy-bucket" in check.detail

    def test_fail_when_no_credentials_are_available(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CB_BUCKET_EXPORT_SOURCE_BUCKET", "v1-legacy-bucket")

        def _raise() -> tuple[object, str | None]:
            raise cutover_runner.gcp_auth.GcsAuthError(
                "no Google credentials found. Run `gcloud auth application-default login`."
            )

        monkeypatch.setattr(cutover_runner.gcp_auth, "export_credentials", _raise)

        check = cutover_runner._check_gcs_export()  # noqa: SLF001 - the check under test

        assert check.status == "fail"
        assert "gcloud auth application-default login" in check.detail

    def test_fail_when_the_bucket_does_not_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CB_BUCKET_EXPORT_SOURCE_BUCKET", "v1-legacy-bucket")
        monkeypatch.setattr(
            cutover_runner.gcp_auth, "export_credentials", lambda: (object(), "fake-project")
        )

        def _raise(bucket_name: str) -> FakeBucketSource:
            raise GcsSourceError("simulated: permission denied")

        monkeypatch.setattr(cutover_runner, "open_bucket_source", _raise)

        check = cutover_runner._check_gcs_export()  # noqa: SLF001 - the check under test

        assert check.status == "fail"
        assert "permission denied" in check.detail
