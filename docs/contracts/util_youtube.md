# Contract: util_youtube (v1 -> v2)

Phase 2/6 of `/migrate-feature` for `/youtube`. QA:
`../Cookiebot-QA/features/util_youtube.feature`. FEATURE-MAP row:
`util_youtube`. Spec/design: `.specs/features/util_youtube/{spec,design,tasks}.md`.
Files owned by this port: `packages/cb-core/src/cb_core/jobs.py`
(`YOUTUBE_SEARCH`), `packages/cb-core/src/cb_core/settings.py`
(`youtube_api_key`, `youtube_timeout_seconds`), `.env.example`,
`packages/cb-gateway/src/cb_gateway/handlers/youtube.py` (new),
`packages/cb-worker/src/cb_worker/jobs/youtube.py` (new),
`packages/cb-worker/src/cb_worker/main.py` (registration), and the tests
listed below.

## Phase 1 — where v1 lives

- Handler: `youtube_search`, `../COOKIEBOT-Telegram-Group-Bot/Bot/SocialContent.py:172-189`.
- Dispatch: `COOKIEBOT.py:248-249,260-261` — the `utilityfunctions`-gated
  `elif` chain (`notify_utility_off` when off, `Miscellaneous.py:133-135`),
  same chain `fun_dice`'s contract already documents in full.
- Locale strings: `youtube_need`, `youtube_no_find` — already ported
  byte-identical, `cb_core/locale_data/{en,pt,es}/lib.json`.

## Phase 2 — v1 behaviour contract

| Aspect | v1 behaviour (file:line) |
|---|---|
| Trigger | `/youtube` (aliased, `cb_core/textmatch.py`, pre-existing) |
| Preconditions | `functionsUtility` only — no admin check |
| Cooldowns / quotas | None (`Cooldowns.py` grepped in full) |
| No query | `len(msg['text'].split()) == 1` ⇒ `youtube_need`, no API call (`:173-176`) |
| Search | `search().list(q=query, part="snippet", type="video", maxResults=10)`, `query = ' '.join(msg['text'].split()[1:])` (`:177-179`) |
| No results | react `🤷` (`is_big=False`), then `youtube_no_find` (`:181-184`) |
| Success | `random.choice` of up to 10 results; reply `f"<i> {video_url} </i>\n\n<b> {video_description} </b>"`, `parse_mode='HTML'` — `video_url` = `https://www.youtube.com/watch?v={videoId}`, `video_description` = the raw snippet description, unescaped/untruncated (`:186-189`) |
| Persistence | None |
| External calls | YouTube Data API v3 `search.list`, **no timeout** in v1 (`googleapiclient`'s bare default — effectively unbounded) |
| Known defects | D-YT-1 below |

## Defects — verdict per item

| id | Defect | Verdict |
|---|---|---|
| D-YT-1 | Synchronous, unbounded external API call directly on the reply path. | **fix** — AGENTS.md §2.4 names an external API call as `cb-worker` work; the gateway does the free synchronous parts (gate, no-query check) and enqueues the rest. A real timeout (`youtube_timeout_seconds`, default 5s) is a v2-only addition, not a preserved value — v1 never bounded this call at all. |

## What moved, and why the reply timing changed

`cb_gateway/handlers/youtube.py` keeps `ctx.enabled("utility")` and the
no-query check (`youtube_need`) — both free, synchronous, matching v1's own
order exactly. A real query enqueues `jobs.YOUTUBE_SEARCH` with `group_id`,
`message_id`, `query`, `lang` — the same scalar-only payload discipline
`util_everyone`/`util_calladms` established, third consumer of that wiring.
`cb_worker/jobs/youtube.py` does the actual `search.list` call (direct
`httpx` GET against the REST endpoint, not `google-api-python-client` — v2
already has `httpx` for every other outbound call, AGENTS.md §5) and sends
the eventual reply itself.

**Consequence**: the reply now comes from `cb-worker`, a queue hop after the
triggering message, rather than synchronously inline. Same shape
`util_everyone`'s fan-out and `util_calladms`'s DM half already established
for "the answer arrives from a different process" — the single-message
output itself is unchanged, only which process sends it and exactly when.

**A request-level failure (bad key, timeout, non-2xx, malformed body) and a
genuine zero-result search both reply `youtube_no_find`** — v1 has no
distinct "the search itself is broken" string, and this port does not
invent one; it degrades to the nearest existing honest string, the same
policy `util_calladms`'s `admin_usernames` and `fun_battle`'s
`battle_extract` already established for a failed external call. Only the
telemetry label (`sent|not_found|error`) distinguishes the two internally.

## Phase 6 — parity table

| Aspect | Verdict |
|---|---|
| Trigger, no-query message, search parameters, "no results" reaction + message, success message shape | **same, byte-identical** |
| `functionsUtility` gate | **same** |
| External call location (reply path vs. worker) | **changed (intentional, fix)** — D-YT-1, AGENTS.md §2.4 |
| Request timeout | **changed (intentional, fix)** — v1 had none; v2 bounds it at 5s (configurable) |
| Reply timing | **changed (unavoidable consequence)** — a queue hop after the trigger, not synchronous; same precedent `util_everyone`/`util_calladms` set |
| A broken search vs. a genuine empty result | **same observable message** (`youtube_no_find` either way), distinguished only in telemetry — v2-only, not user-visible |

## Tests

| Layer | File |
|---|---|
| Unit — trigger surface | `packages/cb-gateway/tests/test_youtube.py` |
| Unit — the API call (success, empty, non-2xx, timeout, malformed JSON, no key), the reply/reaction shape, failure degrading to `youtube_no_find` | `packages/cb-worker/tests/test_youtube_job.py` |
| Acceptance — the QA scenario, enqueue asserted against a fake queue (the worker half is not re-run in this harness — see `qa/features/util_youtube.feature`'s header) | `qa/features/util_youtube.feature`, `qa/test_util_youtube.py` |
