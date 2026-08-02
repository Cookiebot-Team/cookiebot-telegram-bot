# platform_bucket_export — Specify

**Feature id:** `platform_bucket_export` · **Area:** platform · **Kind:** cutover
migration tooling (not a v1-behaviour port — nothing here is user-visible)
**Mirrors:** `cb_worker.importer` (Mongo → Citus). Not yet registered in
`scripts/spec.py` — see `tasks.md` T-final.

## Goal

Move every bot-owned static asset out of v1's private GCS bucket
(`cookiebot-bucket`) and into v2 object storage, once for real on cutover day
and a second time immediately after to catch whatever changed in between.
There is no maintenance window, so this has to be safe to run twice: idempotent
(a re-run duplicates nothing), resumable (a crash mid-run loses nothing already
landed) and auditable (every blob's outcome is on the record for the day it
matters).

This is infrastructure, not a feature port — nothing here is reachable from a
Telegram update. It exists because two in-flight v2 ports are blocked on it:
see "Relationship to fun_death / fun_battle" below.

## Source of truth: what gets copied

Not a prefix list handed down by anyone — grepped from every `list_blobs`,
`get_bucket`, `.blob(...)` call in the v1 checkout
(`../COOKIEBOT-Telegram-Group-Bot`). Two buckets exist in v1
(`Bot/universal_funcs.py:27-28`):

```python
storage_bucket = storage_client.get_bucket("cookiebot-bucket")  # private
storage_bucket_public = storage_client.get_bucket("cookiebot-bucket-public")  # public
```

**`cookiebot-bucket` (private) — this is the one this tool reads.** Every
`list_blobs(prefix=...)` call against it, in source-file order:

| Prefix | v1 file:line | Feeds |
|---|---|---|
| `IdeiaDesenho` | `Bot/Miscellaneous.py:16` | `/drawingidea` |
| `Death` | `Bot/Miscellaneous.py:17` | `/death` (blocks `fun_death`) |
| `Countdown/BFF` | `Bot/Miscellaneous.py:18` | countdown command |
| `Countdown/Patas` | `Bot/Miscellaneous.py:19` | countdown command |
| `Countdown/FurSMeet` | `Bot/Miscellaneous.py:20` | countdown command |
| `Countdown/Furcamp` | `Bot/Miscellaneous.py:21` | countdown command |
| `Countdown/Pawstral` | `Bot/Miscellaneous.py:22` | countdown command |
| `Custom/` | `Bot/Miscellaneous.py:23,147` | `custom_command` — dynamic, one subfolder per custom command name, discovered by listing, never hardcoded |
| `Fight/English` | `Bot/SocialContent.py:24` | `/battle` (blocks `fun_battle`) |
| `Fight/Portuguese` | `Bot/SocialContent.py:25` | `/battle` |

That is the complete set — `grep -rn "list_blobs\|get_bucket\|\.blob(" **/*.py`
across the whole v1 checkout returns nothing else against this bucket.

**Cross-check against the prefix set named in the task request:** `Death/`,
`Fight/English`, `Fight/Portuguese` and all five `Countdown/*` prefixes match
exactly. **`IdeiaDesenho` was not on that list** and is included here anyway —
the source code reads it with exactly the same unconditional
`list(storage_bucket.list_blobs(prefix=...))` at import time as `Death` or
`Fight/English` (`Bot/Miscellaneous.py:16`), so there is no principled reason
to leave it out. `Custom/` matches, described correctly as dynamic.

**Deliberately excluded: `cookiebot-bucket-public`'s `chatpfp/` prefix**
(`Bot/Configurations.py:8,30`). This is a *different bucket*, and v1 itself
writes to it — `blob.upload_from_filename(temp_file)` at
`Configurations.py:31` — caching a chat's Telegram profile photo the first
time `get_group_info` needs one. It is not source-of-truth static content, it
fails the "the source bucket is read-only" premise the whole tool is built
around, and there is nothing in it a byte-for-byte copy is worth preserving:
v2's own chat-photo caching (whenever it exists) repopulates it straight from
Telegram, the way v1's own cache does. This is a design decision, not an
oversight — see `design.md` R1 for the enforcement mechanics this exclusion
depends on (this tool has no credential scoped to the public bucket at all).

## Read-only enforcement — three layers, not one

The source bucket must never be written to, and no single layer is trusted
alone to guarantee that:

1. **OAuth scope.** The GCS client is built from credentials minted for
   exactly `https://www.googleapis.com/auth/devstorage.read_only` —
   narrower than `google.cloud.storage.Client`'s own default, which offers
   `read_only`, `read_write` and `full_control` and lets the credential
   decide. A write call 403s at Google's API layer before it reaches this
   bucket's IAM policy.
2. **Narrow wrapper.** `GcsReadOnlySource` exposes exactly `list_prefix` and
   `download`. No delete, no upload, no patch, and the underlying
   `google.cloud.storage.Bucket`/`Client` handles are private, never returned
   to a caller.
3. **A test that fails the build, not just review**, if either of the above
   regresses — `tests/test_bucket_export.py::TestReadOnlyEnforcement`.

## Idempotency, resumability, auditability

| Property | Mechanism |
|---|---|
| Idempotent | Destination keys are content-hash-addressed (`legacy/v1-bucket/<hh>/<hash><ext>`). "Already copied" is one `store().exists(key)` check — the key *is* the hash, so this is true even for two different source paths holding identical bytes. |
| Resumable | Every blob a run touches gets one line appended to a JSON-Lines manifest, flushed immediately. A later run reads the manifest first and, when a prior line's recorded size still matches the source's and the recorded destination object still exists, skips the **download** too, not just the write. A crash mid-run loses nothing already appended. |
| Auditable | The manifest is the audit trail: source path, content hash, byte size, destination key, outcome, and a timestamp, one line per (blob, run). `--verify` re-reads it and confirms every destination object still exists with the recorded size and content hash. |
| Non-fatal failures | A blob that fails to download (or a whole prefix that fails to list) is recorded as a `"failed"` manifest/report entry and the run continues — never abandons the rest of a run over one bad object. |

## CLI contract

```
python scripts/cb.py bucket-export [--dry-run] [--verify] [--prefixes p1,p2] [--manifest PATH]
```

| Flag | Behaviour |
|---|---|
| (none) | List, download, hash and copy every known prefix; skip what is already present; append the manifest as it goes; print a `rich` progress bar per prefix and a final summary table. |
| `--dry-run` | Same listing, downloading and hashing (so the printed prediction is exact), `store().put()` and the manifest append are the only things skipped. |
| `--verify` | No v1 read at all — re-reads the manifest, confirms every destination object's size and content hash, prints a table of any problem found. |
| `--prefixes p1,p2` | Restrict to a subset, comma-separated, for a targeted re-run. |
| `--manifest PATH` | Manifest file location (default `bucket_export_manifest.jsonl`, overridable, also via `CB_BUCKET_EXPORT_MANIFEST`). |

Configuration is environment-only (`CB_BUCKET_EXPORT_SOURCE_BUCKET`,
`CB_BUCKET_EXPORT_DEST_URI`, `CB_BUCKET_EXPORT_DEST_ENDPOINT`,
`CB_BUCKET_EXPORT_DEST_REGION`) — see `design.md` R5 for why this tool does not
add fields to `cb_core.settings.Settings`.

**Failure output, always actionable, never a traceback:**

- No `CB_BUCKET_EXPORT_DEST_URI` → `error: CB_BUCKET_EXPORT_DEST_URI is not set …`, exit 2.
- No `CB_BUCKET_EXPORT_SOURCE_BUCKET` → `error: no source bucket configured …`, exit 2.
- No usable Google credentials → `error: no Google credentials found for the source bucket. Set GOOGLE_APPLICATION_CREDENTIALS …`, exit 2.
- `--verify` with no manifest on disk yet → `error: no manifest at … — run an export … first`, exit 2.
- A real run or verify pass that completes with one or more per-blob failures/problems → exit 1 (report still printed in full).
- Clean run → exit 0.

## Where the copies land

`cb_core.storage.store()`, not `.media()`. `media_objects.group_id` is
`NOT NULL` and is the Citus distribution column — every blob this tool moves
is bot-owned and global (a `/death` gif is the same gif for every group), not
scoped to any one group, so it has no honest `group_id` to put there. `store()`
is the raw content-addressed blob layer AGENTS.md §5 names for exactly this
case. See `design.md` R2 for the full reasoning and the namespace chosen
(`legacy/v1-bucket/`, separate from `media/...` and `derived/...`).

## Relationship to `fun_death` / `fun_battle`

Both of those in-flight ports are currently `BLOCKED` in their own specs
(`.specs/features/fun_death/spec.md`, `.specs/features/fun_battle/spec.md`) on
exactly this gap: their image pools live only in v1's private bucket, never
checked into any repo. Their own recommendation was to vendor the relevant
prefix byte-for-byte into `packages/cb-core/src/cb_core/asset_data/` (the
package-data pattern `fun_complaint` already established), the same pattern
`cb_core/assets.py` documents.

This tool does not do that vendoring — it moves the bytes out of v1's
soon-to-be-decommissioned GCS project into v2's own object storage, with an
audit trail, which is the actual infrastructure blocker (v1's bucket access is
going away; nothing here waits on that). Whether `fun_death`/`fun_battle` end
up reading their pool from `cb_core.storage.store()` directly or from a
curated `asset_data/` subset pulled out of this tool's output is a decision
for those features' own `design.md`, out of scope here — this spec only
records that this export is the prerequisite either path needs.

## Success criteria

1. Every prefix in "Source of truth" above is read, including `Custom/`'s
   dynamic subfolders, and no others.
2. A run against a fake source with mixed unique/duplicate/failing content
   proves: idempotent (unique content lands exactly once, a re-run copies
   nothing new), resumable (a re-run downloads nothing a prior run already
   landed), auditable (the manifest carries every blob's outcome), and
   non-fatal on a per-blob failure.
3. `GcsReadOnlySource` cannot be made to write under any code path reachable
   from its public surface, and this is asserted by a test, not just true by
   inspection.
4. No credentials in this environment → a one-line, actionable error, no
   traceback, exit code 2.
5. `--verify` catches a destination object that went missing or whose bytes no
   longer match the manifest's recorded hash.
6. Unit tests green, `ruff check`, `ruff format --check` and
   `python scripts/cb.py types` all pass.
