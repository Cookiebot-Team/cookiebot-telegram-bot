# platform_bucket_export — Design

Reads with `spec.md`. Requirement ids are back-referenced from `tasks.md`.
Package: `packages/cb-worker/src/cb_worker/bucket_export/` — same three-layer
split as `cb_worker.importer` (source / mapping-equivalent / runner), with a
fourth layer, the manifest, that the Mongo importer does not need because
Postgres upserts already give it idempotency for free; a filesystem source has
no such backing store, so the manifest is what plays that role here.

## R1 — Source: `source.py`

- **R1.1** `open_source(bucket_name: str) -> GcsReadOnlySource` builds the GCS
  client from `google.auth.default(scopes=["https://www.googleapis.com/auth/devstorage.read_only"])`
  — the read-only scope requested explicitly, never the SDK's default (which
  offers `read_only`, `read_write` *and* `full_control` and lets the credential
  pick). A `DefaultCredentialsError` is caught and re-raised as `GcsSourceError`
  with an actionable message (which env var, what IAM role, the `gcloud`
  fallback) — the CLI must never show a bare SDK traceback for "no
  credentials" (spec.md, "Failure output").
- **R1.2** `GcsReadOnlySource` wraps `google.cloud.storage.Client`/`Bucket` down
  to exactly `list_prefix(prefix) -> Iterator[SourceBlob]` and
  `download(name) -> bytes`. The `Bucket`/`Client` handles are private
  attributes (`_bucket`), never returned by any method — there is no path from
  this object's public surface to `.delete_blob()`, `.blob(...).upload_from_*()`
  or any IAM/ACL call.
- **R1.3** Both methods catch `google.api_core.exceptions.GoogleAPIError` and
  re-raise `GcsSourceError` with the bucket/prefix/name in the message — never
  a raw SDK exception past this module's boundary, so `runner.py` has one
  exception type to catch for "the source misbehaved."
- **R1.4** `SourceBlob` (in `__init__.py`, not `source.py`, so `runner.py` and
  tests can use it without importing anything GCS-specific) carries `name`,
  `size`, `updated`, `md5_hash` — metadata only, from the listing call, no
  bytes. `download` is the only place bytes ever cross this boundary.
- **R1.5** `BucketSource` (a `Protocol` in `__init__.py`) is the seam: `source.py`
  implements it for real GCS, `tests/test_bucket_export.py::FakeBucketSource`
  implements it for everything else. Same split `cb_worker.importer.source`
  uses for `LiveMongoSource`/`DumpMongoSource` against `MongoSource`.
- **R1.6** Using `google-cloud-storage`/`google-auth` directly here, rather than
  `cb_core.storage` (which wraps `obstore`), is a deliberate exception to
  AGENTS.md §5's "never touch a cloud SDK directly" — that rule governs how
  *our own* storage is read and written; this is a one-time read of a foreign,
  soon-to-be-decommissioned system, the same call `cb_worker.importer.source`
  already made for v1's MongoDB (`pymongo` there, `google-cloud-storage` here).
  `obstore`'s `GCSStore` has no OAuth-scope-restriction knob, which is the one
  thing spec.md's read-only requirement is actually built on — even if it did,
  swapping it in would not change the two other enforcement layers (R1.2, the
  test in R6).

## R2 — Keys and destination: `keys.py`

- **R2.1** `destination_key(content_hash, source_name) -> str` returns
  `legacy/v1-bucket/<hh>/<hash><ext>` — `<hh>` the first two hex characters of
  the hash (same fan-out convention as `cb_core.storage.keys.blob_key`), `<ext>`
  taken from `source_name`'s own suffix so the object is still
  openable/previewable by extension.
- **R2.2** A namespace of its own (`legacy/v1-bucket/`), not `media/<kind>/...`:
  these blobs have no `media` `kind` (`VALID_KINDS` is a Telegram-update
  vocabulary — photo/sticker/animation/…, not a static-asset one) and no
  `group_id` to scope a `media_objects` row to. Reusing `media/` would either
  invent a fake kind or collide with a real media key; a separate namespace
  avoids both.
- **R2.3** `store()`, not `.media()` — restated from spec.md because it is the
  one decision most worth a second opinion. `MediaService.put` requires a
  `group_id` (the Citus distribution column on `media_objects`) and a `kind`
  from a closed vocabulary; every blob this tool moves is bot-owned and global
  (the same `/death` gif for every group), so `group_id` has no honest value.
  `cb_core.storage.store()` — the raw content-addressed blob layer AGENTS.md §5
  names for exactly "raw blobs" — is the correct fit, and content-addressing at
  the key level (R2.1) gets this tool the same dedupe property `media()` would
  have given it, without a fabricated per-group reference row.
- **R2.4** Hash algorithm: blake3 via `cb_core.dedupe.fingerprint`, the same
  function `cb_core.storage.keys.hash_and_key` uses — one house hash for
  content-addressing across the codebase, not a second one. GCS's own
  `md5_hash` (visible for free on every listing) is deliberately *not* used as
  the destination key's basis: it is absent for composite/CMEK objects, and
  trusting a foreign system's hash for our own content-addressing would be a
  second hash algorithm to reason about for no real gain, since every blob is
  downloaded and hashed anyway to know whether it is a duplicate (R4.2).

## R3 — Manifest: `manifest.py`

- **R3.1** Format: JSON Lines, one object per line, append-only. Fields:
  `prefix`, `source_path`, `byte_size`, `content_hash`, `destination_key`,
  `outcome` (`"copied" | "skipped" | "failed"`), `detail`, `exported_at`
  (ISO-8601 UTC). Chosen over a single JSON array or a database table
  specifically for the append-and-flush durability property (R3.2) and because
  an ops engineer on cutover day needs `grep`/`jq`, not a script, to answer "did
  this one blob copy" — plain text over a binary or DB-backed format for the
  same "auditable by a human" reason the requirement asks for a summary table
  at all.
- **R3.2** `append(path, entry)` opens in append mode, writes one line, flushes
  before returning — the durability point a resumed run relies on. A process
  killed mid-run loses at most the one blob in flight, never anything already
  recorded.
- **R3.3** `latest_by_source(path) -> dict[str, ManifestEntry]` folds the file
  keeping the last line per `source_path` — "last write wins," the same
  semantics `TableLoad`-driven upserts use elsewhere in this codebase. This is
  what `runner.run_export` reads at the start of a run (resumability) and what
  `verify_manifest` reads for `--verify`.
- **R3.4** `path_exists(path) -> bool` exists purely so `verify_manifest` (an
  `async def`) never calls a `pathlib.Path` method directly in its own body —
  ruff's `ASYNC240` flags that pattern; wrapping it in a plain sync helper
  sidesteps the lint for what is a one-time, few-KB stat, not a hot path
  worth reaching for `anyio.Path`/`trio.Path` over.

## R4 — Runner and rich presentation: `runner.py`

- **R4.1** `_process_blob(source, store, prefix, blob, previous, *, dry_run)` is
  the unit both `run_export` and the resumability logic live in: check the
  prior manifest entry first (R4.3); on a miss, `source.download`; on a
  download failure, return a `"failed"` entry (never raise past this function —
  that is what makes a bad blob non-fatal to the run, spec.md's "Non-fatal
  failures"); otherwise hash, derive the destination key (R2.1), check
  `store().exists()`, and either skip (already present) or copy (or, under
  `dry_run`, predict "would copy" without writing).
- **R4.2** Every blob is downloaded and hashed exactly once per run, whether or
  not it ends up written — dedupe (R2.3) and the printed `dry-run` prediction
  both need the real content hash, and there is no cheaper source-side signal
  trusted enough to substitute (R2.4).
- **R4.3** Resumability fast path: if the manifest's last entry for this
  `source_path` says `"copied"`/`"skipped"`, the source's *currently listed*
  size still matches the size recorded then, and `store().exists()` on the
  recorded destination key still succeeds, skip the download entirely and
  re-emit a `"skipped"` entry. This is strictly an optimisation over R4.1's
  content-hash check (which is already sufficient for correctness) — it is
  what makes a resumed run cheap, not just correct. A size change is enough to
  fall through to a full re-download; a same-size content change on the source
  side within one export window is accepted as out of scope, the same kind of
  trade-off `cb_worker.importer` makes by trusting v1's Mongo documents as
  they are read.
- **R4.4** `run_export(source, store, *, prefixes=PREFIXES, manifest_path,
  dry_run=False, console=None) -> ExportReport` — one `rich.progress.Progress`
  bar per prefix (materialising each prefix's listing up front, same eager
  `list(...)` v1's own bucket code does, acceptable at this corpus's size), one
  `PrefixStats` row per prefix regardless of whether it found anything. A
  prefix that fails to *list* (not per-blob, the whole prefix) is caught,
  logged and recorded as a `"failed"` report entry with `source_path="*"` —
  same non-fatal contract at the prefix level as at the blob level.
- **R4.5** `verify_manifest(store, manifest_path) -> VerifyReport` — no source
  read at all. For every `"copied"`/`"skipped"` manifest entry (a `"failed"`
  entry never had a destination), `store().get()` the object, compare length to
  the recorded `byte_size`, and blake3 the bytes and compare to the recorded
  `content_hash`. Downloads the full object rather than trusting the
  content-addressed key alone, because "confirms … the expected size and hash"
  (spec.md) means proving the bytes still match, not just that something is
  present under the right name — cheap at this corpus's total size (spec.md
  cites ~112 MB for a full run).
- **R4.6** `render_summary`/`render_verify` build `rich.table.Table`s — prefix,
  found, copied, skipped, failed, bytes (human-formatted, no new dependency for
  that), in the order `run_export` processed prefixes, plus a bold `TOTAL` row.
  `render_verify` lists every problem, or one "all N verified" row when clean.

## R5 — CLI and configuration: `__main__.py`, `scripts/cb.py`

- **R5.1** `python -m cb_worker.bucket_export [--dry-run] [--verify]
  [--prefixes p1,p2] [--manifest PATH]`, wired to `python scripts/cb.py
  bucket-export` next to `import-mongo`, same `@task(...)` shape and the same
  "print the reproduction command" convention `run()` already gives every task.
- **R5.2** Configuration is environment-only
  (`CB_BUCKET_EXPORT_SOURCE_BUCKET`, `CB_BUCKET_EXPORT_DEST_URI`,
  `CB_BUCKET_EXPORT_DEST_ENDPOINT`, `CB_BUCKET_EXPORT_DEST_REGION`,
  `CB_BUCKET_EXPORT_MANIFEST`) rather than new fields on
  `cb_core.settings.Settings`. Two reasons, not one: (a) this task's file list
  does not include `packages/cb-core/src/cb_core/settings.py` — another agent
  owns that surface concurrently; (b) more durably, this tool's destination is
  deliberately *not* the same knob the running app's `init_storage()` uses
  (`settings.storage_uri`, no endpoint/region support today) — see R5.3. If a
  future task points the live bot's own storage at R2 (adding endpoint/region
  support to `Settings`/`init_storage`), the natural convergence is to set
  `CB_BUCKET_EXPORT_DEST_URI`/`_ENDPOINT`/`_REGION` to the same values as
  whatever that task adds, not to remove this tool's own knobs.
- **R5.3** The destination store is built with
  `cb_core.storage.store_from_uri(uri, **options)` called directly — still
  "through `cb_core.storage`" (AGENTS.md §5), just not through the
  `init_storage()`/`store()` app-wide singleton, which is keyed off the
  running app's own `settings.storage_uri` and today passes no extra options
  at all (`cb_core/storage/__init__.py:36`). A one-shot migration CLI owning
  its destination configuration independently is the same shape
  `import-mongo` already uses for its Mongo source (`settings.mongo_uri`/
  `mongo_dump_dir`, read only by the importer, not by the app's runtime DB
  pool config).
- **R5.4** R2 (Cloudflare R2) is S3-compatible and reached as `s3://<bucket>`
  through `store_from_uri`'s existing `**options` passthrough to `S3Store`
  (`obstore_backend.py`'s own docstring: "region, endpoint, anonymous access,
  … so a MinIO or fake-GCS endpoint works in CI" — R2 is the same mechanism,
  a real cloud instead of a CI fake). `CB_BUCKET_EXPORT_DEST_ENDPOINT` supplies
  `endpoint`, `CB_BUCKET_EXPORT_DEST_REGION` supplies `region` (R2's own
  convention is `"auto"`).
- **R5.5** Exit codes: `2` for a configuration/credentials error (caught in
  `main()` as `ValueError | GcsSourceError`, printed as `error: …`, no
  traceback — spec.md's "Failure output"); `1` when a run or verify pass
  completes but found at least one failure/problem (report still printed in
  full first); `0` otherwise. Mirrors `import-mongo`'s `ValueError` handling,
  with the addition of a non-zero-but-not-2 code for "ran fine, found
  problems," which `import-mongo` does not need because a Mongo document
  either maps or is counted `Skipped`, never "failed" in a way ops needs
  paged for.

## R6 — Tests: `tests/test_bucket_export.py`

- **R6.1** `FakeBucketSource` implements `BucketSource` in memory, tracks
  `download_calls` (what the resumability tests assert against) and supports a
  `failing` set of names for the non-fatal-failure tests.
- **R6.2** Destination is `cb_core.storage.store_from_uri("memory://")` — real
  code, not a mock, per AGENTS.md §6 ("mock the outside world only").
- **R6.3** `TestReadOnlyEnforcement` is the one class exercising the real
  `GcsReadOnlySource`/`open_source` (spec.md's success criterion 3): asserts
  the public method set is exactly `{list_prefix, download, close}`, that no
  method name contains a write verb, that `open_source` requests exactly the
  read-only scope (`google.auth.default`/`google.cloud.storage.Client`
  monkeypatched, no real credentials or network involved), and that a missing
  credential and an empty bucket name both raise the documented, actionable
  errors.
- **R6.4** Coverage beyond enforcement: dry-run writes nothing; a real run
  writes the store and the manifest; a second run against the same manifest
  downloads nothing and copies nothing (idempotent + resumable, one test each
  assertion); identical bytes under two different source paths dedupe to one
  `store().put()`; a failing blob is counted, not fatal, and recorded with
  `outcome="failed"`; `verify_manifest` confirms a clean export, detects a
  deleted destination object, detects a tampered/mismatched hash, and raises a
  clear error when no manifest exists yet.

## R7 — Docs

- **R7.1** `docs/site/content/docs/cutover-bucket-export.mdx` — what it does,
  how to run it on the day (dry-run → run → verify), the read-only guarantee
  and its three enforcement layers, the full prefix inventory with the
  `IdeiaDesenho` and `chatpfp` notes, and what to do when a blob fails.
- **R7.2** This spec/design/tasks triad, per `tlc-spec-driven`.
- **R7.3** Not done here, deliberately: registering `platform_bucket_export` in
  `scripts/spec.py` and running `cb.py docs-sync` — both are explicitly
  off-limits for this task (owned by a concurrent agent). Flagged as a
  follow-up in `tasks.md` T-final.

## Open decisions — answered

1. **Does `IdeiaDesenho` belong in the prefix set even though it was not named
   in the request?** Yes (spec.md, "Source of truth") — the v1 source code
   reads it exactly as unconditionally as every prefix that *was* named; the
   instruction to "grep the checkout … cross-check … rather than replace" is
   explicit about this exact situation.
2. **Copy `chatpfp` from the public bucket too, since it is technically also a
   bot-owned asset in a bucket named `cookiebot-bucket*`?** No (spec.md,
   "Deliberately excluded") — different bucket, v1 writes to it itself, so it
   fails the read-only premise and is not source-of-truth content in the first
   place.
3. **`store()` or `.media()` for the destination?** `store()` (R2.3) — no
   group to scope a `media_objects` row to.
4. **Add destination config to `cb_core.settings.Settings`?** No (R5.2) — out
   of this task's file list, and arguably the wrong long-term owner anyway
   since this tool's destination need not be the same bucket the live app's
   `storage_uri` points at.
5. **Trust GCS's own `md5_hash` for dedupe instead of downloading and
   blake3-hashing?** No (R2.4) — every blob has to be downloaded and hashed
   anyway to predict `--dry-run` accurately and to catch a same-size content
   change, so there is no real cost saved, and it would be a second hash
   algorithm in a codebase that otherwise has one.
6. **Does this tool also vendor `Death/`/`Fight/*` into
   `cb_core/asset_data/` and unblock `fun_death`/`fun_battle` outright?** No
   (spec.md, "Relationship to `fun_death`/`fun_battle`") — it removes the
   actual infrastructure blocker (bytes only reachable through v1's
   soon-to-vanish GCS project) but the choice of how those two features
   consume the result belongs in their own design docs, not here.
