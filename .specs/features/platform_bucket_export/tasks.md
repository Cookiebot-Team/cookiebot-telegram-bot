# platform_bucket_export — Tasks

Grammar: `tlc-spec-driven` §5. Read `spec.md` and `design.md` first.

Built in one pass by a single agent rather than dispatched task-by-task
(the requesting brief scoped a bounded file list and a single gate command),
so this file is written retrospectively, in the same grammar, to keep the
artifact trail intact for whoever reads it next.

## Status

| Task | Status | Notes |
|------|--------|-------|
| T1 — Read-only GCS source | ✅ done | scope + narrow wrapper + enforcement test |
| T2 [P] — Destination keys and manifest | ✅ done | content-addressed, JSONL audit trail |
| T3 — Runner: idempotent, resumable, rich output | ✅ done | depends on T1, T2 |
| T4 — CLI and `scripts/cb.py` wiring | ✅ done | depends on T3 |
| T5 [P] — Tests | ✅ done | written alongside T1–T4, not after |
| T6 — Docs page | ✅ done | depends on T1–T4 |
| T-final — Close out | 🚫 blocked: scripts/spec.py and docs-sync are out of scope for this task | see below |

## Tasks

### T1 — Read-only GCS source

- **Skills:** none (infrastructure, no v1 behaviour to port)
- **What:** `GcsReadOnlySource`/`open_source` per design R1. Credentials scoped
  to `devstorage.read_only` explicitly; the class exposes only `list_prefix`
  and `download`; both wrap `GoogleAPIError` as `GcsSourceError`; missing
  credentials/bucket name fail with an actionable message, not a traceback.
- **Where:** `packages/cb-worker/src/cb_worker/bucket_export/__init__.py` (the
  `BucketSource` protocol, `SourceBlob`), `.../bucket_export/source.py`.
- **Depends on:** none
- **Reuses:** the `MongoSource`/`LiveMongoSource` split in
  `cb_worker/importer/{__init__,source}.py` as the shape to mirror.
- **Done when:** `TestReadOnlyEnforcement` (T5) passes — public method set,
  no write-verb method name, exact scope requested, clear errors for missing
  bucket/credentials.
- **Gate:** `uv run pytest packages/cb-worker/tests/test_bucket_export.py::TestReadOnlyEnforcement -q`
- **Commit:** `feat(cb-worker): read-only source for v1's GCS bucket`
- **→ R1**

### T2 [P] — Destination keys and manifest

- **Skills:** none
- **What:** `destination_key(content_hash, source_name)` per design R2
  (content-addressed, `legacy/v1-bucket/<hh>/<hash><ext>`); `manifest.append`/
  `read_all`/`latest_by_source`/`path_exists` per design R3 (append-only JSONL,
  last-line-wins folding, missing-file-is-empty, no `pathlib` call inside an
  `async def`).
- **Where:** `packages/cb-worker/src/cb_worker/bucket_export/keys.py`,
  `.../bucket_export/manifest.py`.
- **Depends on:** none (independent of T1)
- **Reuses:** `cb_core.storage.keys.blob_key`'s two-hex-char fan-out
  convention; `cb_core.dedupe.fingerprint` (blake3) as the one house hash.
- **Done when:** a round-trip through `append`/`read_all` returns the entry
  unchanged, `latest_by_source` keeps the last line per source path, and
  `destination_key` is stable for identical bytes regardless of source path.
- **Gate:** `uv run pytest packages/cb-worker/tests/test_bucket_export.py::TestKeys packages/cb-worker/tests/test_bucket_export.py::TestManifest -q`
- **Commit:** `feat(cb-worker): content-addressed keys and the export manifest`
- **→ R2, R3**

### T3 — Runner: idempotent, resumable, rich output

- **Skills:** none
- **What:** `_process_blob`, `run_export`, `verify_manifest`,
  `render_summary`/`render_verify` per design R4. Resumability fast path
  (manifest match + size match + destination still present skips the
  download); content-hash dedupe on every blob regardless; per-blob and
  per-prefix failures counted, never fatal; `--verify` re-downloads and
  re-hashes every recorded destination object rather than trusting the key
  alone.
- **Where:** `packages/cb-worker/src/cb_worker/bucket_export/runner.py`.
- **Depends on:** T1, T2
- **Reuses:** `cb_worker.importer.runner`'s "never abort the run over one bad
  unit" shape; `rich.progress.Progress`/`rich.table.Table`.
- **Done when:** dry-run predicts without writing; a real run followed by a
  second real run against the same manifest copies nothing new and downloads
  nothing on the second pass; identical bytes under two source paths dedupe to
  one write; a failing blob is reported, not fatal; `verify_manifest` catches
  a missing object and a hash mismatch and refuses to run with no manifest.
- **Gate:** `uv run pytest packages/cb-worker/tests/test_bucket_export.py::TestRunExport packages/cb-worker/tests/test_bucket_export.py::TestVerifyManifest -q`
- **Commit:** `feat(cb-worker): drive the bucket export, idempotently and resumably`
- **→ R4**

### T4 — CLI and `scripts/cb.py` wiring

- **Skills:** none
- **What:** `python -m cb_worker.bucket_export` per design R5 — env-var
  configuration (`CB_BUCKET_EXPORT_SOURCE_BUCKET`/`_DEST_URI`/`_DEST_ENDPOINT`/
  `_DEST_REGION`/`_MANIFEST`), `store_from_uri()` called directly with R2's
  endpoint/region as extra options, exit codes `0`/`1`/`2` per spec.md. One
  `@task("bucket-export", ...)` in `scripts/cb.py` next to `import-mongo`,
  same decorator conventions.
- **Where:** `packages/cb-worker/src/cb_worker/bucket_export/__main__.py`,
  `scripts/cb.py` (one task registration, no other change).
- **Depends on:** T3
- **Reuses:** `cb_worker/importer/__main__.py`'s thin-CLI/`ValueError`-catch
  shape; `scripts/cb.py`'s `task()`/`run()` helpers, unchanged.
- **Done when:** `scripts/cb.py --list` shows `bucket-export`; running it with
  no `CB_BUCKET_EXPORT_DEST_URI` set prints a one-line `error: …` and exits 2,
  never a traceback.
- **Gate:** `uv run python scripts/cb.py bucket-export --dry-run` (expect a clear config error, no traceback, in an environment with no GCS credentials)
- **Commit:** `feat(cb-worker): wire the bucket export into scripts/cb.py`
- **→ R5**

### T5 [P] — Tests

- **Skills:** none
- **What:** `tests/test_bucket_export.py` per design R6 — `FakeBucketSource`,
  `store_from_uri("memory://")` as the real (not mocked) destination,
  `TestReadOnlyEnforcement`, `TestKeys`, `TestManifest`, `TestRunExport`,
  `TestVerifyManifest`, `TestPrefixInventory` (guards the derived prefix set
  itself against a silent edit).
- **Where:** `packages/cb-worker/tests/test_bucket_export.py`.
- **Depends on:** T1, T2, T3 (written alongside them in practice, not after)
- **Reuses:** `packages/cb-worker/tests/test_importer_source.py`'s
  fake-source-over-protocol style; `packages/cb-core/tests/test_storage.py`'s
  `store_from_uri("memory://")` fixture pattern.
- **Done when:** all pass, no live GCS/R2 credentials or network involved.
- **Gate:** `uv run pytest packages/cb-worker/tests/test_bucket_export.py -q`
- **Commit:** folded into T1–T4's commits (each task's own gate already covers
  its slice of this file); listed separately here only because design R6 is
  its own section.
- **→ R6**

### T6 — Docs page

- **Skills:** none
- **What:** `docs/site/content/docs/cutover-bucket-export.mdx` — what the tool
  does, the dry-run → run → verify sequence for the actual cutover day, the
  read-only guarantee and its three enforcement layers, the full derived
  prefix inventory (including the `IdeiaDesenho` finding and the `chatpfp`
  exclusion, both explained), and what an operator does when a blob fails
  (re-run is safe; `--prefixes` to retry narrowly; the manifest's `detail`
  field has the reason).
- **Where:** `docs/site/content/docs/cutover-bucket-export.mdx` (new).
- **Depends on:** T1, T2, T3, T4
- **Reuses:** the structure of other `docs/site/content/docs/*.mdx` operational
  pages (frontmatter `title`/`description`, no hand-edited `status:` field —
  this page is not a feature page and is not touched by `docs-sync`).
- **Done when:** the page exists and matches this triad's content; not linked
  into `docs/site/content/progress.json` or any feature page (both off-limits
  for this task).
- **Gate:** none (prose; `docs-build` belongs to whoever runs the full docs
  pipeline, out of scope here)
- **Commit:** `docs(cutover-bucket-export): how to run it on the day`
- **→ R7.1**

### T-final — Close out

- **Skills:** none
- **What:** Normally: register `platform_bucket_export` in `scripts/spec.py`,
  run `cb.py docs-sync`, and run the full `cb.py check` gate. **All three are
  explicitly out of scope for this task** — `scripts/spec.py` and
  `docs-sync`/`docs/site/content/progress.json`/
  `docs/site/content/docs/features/**` are all on the "do not touch" list for
  this file list (a concurrent agent owns them), and `cb.py check` was
  explicitly excluded from the gate this task was given. Left as a genuine
  follow-up, not silently skipped:
  1. Add a `Feature("platform_bucket_export", "platform", "Cutover export of
     v1's GCS bucket into v2 object storage", "<milestone>", Status.DONE,
     Layer.PLATFORM, ...)` row to `scripts/spec.py`, area `platform`, next to
     `platform_storage`/`platform_selfhosted_api`.
  2. Run `python scripts/cb.py docs-sync`.
  3. Run the targeted gate this task *was* given (below) and, separately, the
     full `python scripts/cb.py check` once the two items above have landed.
- **Where:** `scripts/spec.py` (not touched by this task), regenerated
  `docs/site/**` (not touched by this task).
- **Depends on:** T1–T6
- **Reuses:** n/a
- **Done when:** the follow-up above lands in a session that owns those files.
- **Gate:** `uv run pytest packages/cb-worker/tests/test_bucket_export.py -q && uv run python scripts/cb.py types && uv run ruff check . && uv run ruff format --check .`
- **Commit:** n/a — this task's actual commit boundary is T1–T6; the
  registration follow-up gets its own commit when it lands.
- **→ R7.3**
