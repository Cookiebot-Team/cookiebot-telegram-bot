# Contract: core_musicdetection (v1 -> v2)

Phase 2/6 of `/migrate-feature` for v1's voice-note song identification.
**No QA scenario exists** — `qa/features/core_musicdetection.feature` is
authored as part of this port (AGENTS.md §5). FEATURE-MAP row:
`core_musicdetection`. Spec: `.specs/features/core_musicdetection/spec.md`.

Files owned by this port:
`packages/cb-core/src/cb_core/jobs.py` (`IDENTIFY_MUSIC`),
`packages/cb-core/src/cb_core/settings.py`
(`music_detection_enabled`, `music_detection_timeout_seconds`, `audd_api_key`),
`.env.example`,
`packages/cb-gateway/src/cb_gateway/handlers/musicdetection.py` (new),
`packages/cb-gateway/src/cb_gateway/handlers/__init__.py` (registration),
`packages/cb-worker/src/cb_worker/music.py` (new),
`packages/cb-worker/src/cb_worker/jobs/music.py` (new),
`packages/cb-worker/src/cb_worker/main.py` (registration), and the tests below.

## Phase 1 — where v1 lives

- Handler: `identify_music`, `../COOKIEBOT-Telegram-Group-Bot/Bot/Audio.py:6-20`.
- Dispatch: `COOKIEBOT.py:155-159` — the `voice` content-type branch,
  `if utilityfunctions:`, no `else`.
- Strings: **not in the locale catalog.** Both answers are f-strings in
  `Audio.py:18-20`.

## Phase 2 — v1 behaviour contract

| Aspect | v1 behaviour (file:line) |
|---|---|
| Trigger | **Passive.** Every voice note in a non-private chat (`COOKIEBOT.py:155`) |
| Preconditions | `functionsUtility`; nothing else — no admin check, no cooldown, no duration cap |
| Downloads | `get_media_content(..., 'voice')` — the whole note, before any check |
| Recognition | `ShazamAPI.Shazam(content).recognizeSong()`, first item of the generator (`:7-11`) |
| Generator empty | `StopIteration` ⇒ silent return (`:10-11`) |
| No `track` key | silent return (`:14-15`) |
| Match | reply `f"{SONG\|MÚSICA}: 🎵 <b> {title} </b> - <i> {subtitle} </i> 🎵"` (`:18-20`) |
| Language split | `if language in ['pt', 'es']` ⇒ `MÚSICA`, else `SONG` — **Spanish groups get the Portuguese word** |
| Coexistence | The same `voice` branch then runs the transcribe→AI sub-step under `functionsFun` (`COOKIEBOT.py:160-162`). Both fire for one note. |
| Persistence | None |
| External calls | Shazam's unpublished recognition endpoint, **no timeout, no breaker** |
| Known defects | D-MD-1 … D-MD-3 below |

## Defects — verdict per item

| id | Defect | Verdict |
|---|---|---|
| D-MD-1 | **An unbounded call to an unofficial endpoint on the busiest passive path in the bot.** No timeout, no breaker, run inline on a handler thread — one of the four causes FEATURE-MAP §5 gives for v1's fixed 50-thread pool wedging. | **fix** — `jobs.IDENTIFY_MUSIC` in `cb-worker`, bounded by `music_detection_timeout_seconds`, behind a `cb_core.breaker.Breaker`. The breaker matters more here than for `/youtube` or `/buscarfonte`: those fire on a command, this fires on every voice note, so an outage without one means one doomed call per note forever. |
| D-MD-2 | **No opt-out.** The feature is on for every group with utility functions enabled, calling a third party with user audio. | **fix** — two independent switches: `CB_MUSIC_DETECTION_ENABLED` (default `false`) and an empty `CB_AUDD_API_KEY`, either of which makes the feature inert. Same "unset key means the feature is not there" rule `util_youtube` and `x_reverse_search` already follow. |
| D-MD-3 | **A Spanish group is answered in Portuguese** (`language in ['pt', 'es']`). | **preserve** — this is what live groups see, and there is no catalog key to translate against. Inventing a Spanish string here would be a copy change dressed up as a port. Recorded, and asserted by a test so it cannot be "fixed" by accident. |

## Substitution: the recogniser is not Shazam's

`scripts/spec.py`'s row said "ShazamAPI is unofficial — feature-flag it behind
a breaker". Investigating that turned up something stronger:

* **`ShazamAPI` is unmaintained.** Its maintained successor is `shazamio`,
  whose fingerprinting is a Rust extension, `shazamio-core`.
* **`shazamio-core` cannot be loaded on this workspace's Python.** `python -c
  "import shazamio_core"` **segfaults** on 3.14 (the workspace requires
  ≥3.13), and its `pydub` dependency additionally needs the `audioop` module
  removed in 3.13. A segfault is not containable by `try/except ImportError`,
  so shipping it even as an optional extra would put a process-killing import
  one `pip install` away.

So the port keeps v1's behaviour and changes the vendor to
[AudD](https://audd.io)'s **documented** HTTP API, called through the `httpx`
client this codebase already uses — no new dependency at all. Same trade
`util_postforwarder` already made when it replaced Google Cloud Translate with
the LLM router: same contract, different provider, no new SDK.

`cb_worker.music.set_recogniser` stays the seam for a deployment that has a
working Shazam binding on a Python where it loads; `Recogniser` is a one-method
Protocol.

## Preserved deliberately

- **Passive, with no reply on any failure path.** No match, no key, no
  recogniser, breaker open, download failed — all silent. v1's own no-match
  branch is silent, and a group must never be told about a deployment gap.
- **The answer strings**, byte for byte, `parse_mode="HTML"`, as a reply to
  the voice note.
- **Coexistence with the transcribe→AI sub-step.** The handler raises
  `SkipHandler` on *every* path including the one that enqueued, and is
  registered ahead of `transcribe.router`. Consuming the update here would
  silently disable `x_speech_to_text`'s shape (a) — nothing would error.

## Phase 6 — parity

| Behaviour | v1 | v2 | Same? |
|---|---|---|---|
| Passive on every voice note | yes | yes, when enabled | ⚠️ D-MD-2 |
| Utility gate, silent when off | yes | yes | ✅ |
| No match ⇒ silence | yes | yes | ✅ |
| Match ⇒ reply | `SONG`/`MÚSICA` HTML string | identical | ✅ |
| `es` answered in Portuguese | yes | yes | ✅ (D-MD-3, deliberate) |
| Also runs the AI sub-step | yes | yes | ✅ |
| Recogniser | Shazam, unofficial, unbounded | AudD, documented, bounded, breakered | ⚠️ by substitution |

## Tests

| Layer | File |
|---|---|
| Unit | `packages/cb-worker/tests/test_music_job.py` — response parsing, both answer strings, the silent branches, and the breaker opening and closing |
| Unit | `packages/cb-gateway/tests/test_musicdetection.py` — the handler yields on every path |
| Acceptance | `qa/features/core_musicdetection.feature` + `qa/test_core_musicdetection.py` — five scenarios, including the one that proves the update still reaches `transcribe.voice_ai` |
