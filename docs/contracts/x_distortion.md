# Contract: x_distortion (v1 -> v2)

Phase 2/6 of `/migrate-feature` for `/destroy`. **No QA scenario exists** —
`qa/features/x_distortion.feature` is authored as part of this port
(AGENTS.md §5). FEATURE-MAP row: `x_distortion`. Spec:
`.specs/features/x_distortion/spec.md`.

Files owned by this port:
`packages/cb-core/src/cb_core/textmatch.py` (three aliases),
`packages/cb-core/src/cb_core/jobs.py` (`DISTORT_MEDIA`),
`packages/cb-core/src/cb_core/settings.py` (`distortion_concurrency`),
`packages/cb-core/src/cb_core/locales.py` (`nested_value`/`get_nested`),
`.env.example`,
`packages/cb-gateway/src/cb_gateway/handlers/destroy.py` (new),
`packages/cb-gateway/src/cb_gateway/handlers/__init__.py` (registration),
`packages/cb-worker/src/cb_worker/distort.py` (new),
`packages/cb-worker/src/cb_worker/jobs/distortion.py` (new),
`packages/cb-worker/src/cb_worker/main.py` (registration),
`packages/cb-worker/pyproject.toml` (numpy),
`qa/mock_telegram.py` (`sendAudio`, and the per-scenario reset of
`profile_photos`/`member_counts`), and the tests listed below.

## Phase 1 — where v1 lives

- Handler: `destroy`, `../COOKIEBOT-Telegram-Group-Bot/Bot/Miscellaneous.py:377-433`.
- Pipeline: `Bot/Distortioner.py` — `process_image` (`:37-44`),
  `distort_audiofile` (`:106-108`), `distortioner` (`:110-165`).
- Dispatch: `COOKIEBOT.py:216-217,242-243` — the `funfunctions` chain
  (`notify_fun_off` when off).
- Locale strings: the nested `destroy` object (`instru`/`video`/`gif`) in
  `cb_core/locale_data/{en,pt,es}/lib.json`, already ported byte-identical and
  complete in all three languages.

## Phase 2 — v1 behaviour contract

| Aspect | v1 behaviour (file:line) |
|---|---|
| Trigger | `/destroy`, `/zoar`, `/destruir` (`COOKIEBOT.py:217,242`) |
| Preconditions | `functionsFun` only — no admin check, no cooldown (`Cooldowns.py` grepped in full) |
| `…pfp` | `msg['text'].endswith('pfp')` — the whole message, so `/destroy my pfp` matches too; distorts the **caller's** profile photo, `sendPhoto` (`:379-392`) |
| No reply | `destroy.instru` (`:393-394`) |
| Reply is a video | `destroy.video` — **distortion is switched off**, the frame pipeline behind it is unreachable (`:395-397`) |
| Reply is a photo | largest size, `process_image(..., 25)`, `sendPhoto` (`:398-403`) |
| Reply is audio or voice | `distort_audiofile(..., 10, 1)`, `sendAudio` for both (`:405-416`) |
| Reply is a sticker | animated or video ⇒ `destroy.gif`; otherwise `process_image(..., 25)` to PNG and `sendSticker` (`:417-427`) |
| Reply is an animation | `destroy.gif` (`:428-430`) |
| Anything else | `destroy.instru` (`:431-433`) |
| Image algorithm | `wand`'s `liquid_rescale` to 25% of each dimension, then `resize` back to the original (`Distortioner.py:37-44`) |
| Audio algorithm | ffmpeg `vibrato=f=10:d=1` (`Distortioner.py:106-108`) |
| Concurrency | `while SEMAPHORE_IMAGES: pass` / `SEMAPHORE_AUDIOS` — busy-wait on a module global (`:114,145,155`) |
| Temp files | fixed names in the working directory: `distorted.jpg`, `distorted.png`, `distorted.mp3`, and the downloaded input under its own Telegram filename |
| External calls | `requests.get(f"https://api.telegram.org/file/bot{token}/{path}")` on the `pfp` path (`:383`), no timeout on ffmpeg |
| Known defects | D3, D4 (FEATURE-MAP) and D-DS-1 … D-DS-3 below |

## Defects — verdict per item

| id | Defect | Verdict |
|---|---|---|
| D3 | **Busy-wait spin lock serialises all distortion.** Three module-global booleans, each waited on with an empty `while` loop, so a second concurrent `/destroy` burned a core doing nothing. | **fix** — an `asyncio.Semaphore` in `cb_worker/jobs/distortion.py`, sized by `CB_DISTORTION_CONCURRENCY` (default 2). `test_distortion_job.py::test_concurrency_is_bounded` asserts the bound is real. |
| D4 | **Fixed temp filenames raced across requests.** `distorted.jpg`/`distorted.mp3` are written into the process's working directory by every caller. | **fix** — the image arm never touches disk at all (bytes in, bytes out); the audio arm uses a per-call `TemporaryDirectory`. |
| D-DS-1 | **The whole pipeline ran on the reply path**, including an unbounded ffmpeg subprocess. | **fix** — AGENTS.md §2.4. The gateway keeps only the branch decisions, which are free and made in v1's own order; the download, carve and ffmpeg pass are `jobs.DISTORT_MEDIA`. **Consequence:** the distorted media now arrives from `cb-worker` a queue hop later, the same shape `util_youtube`/`util_everyone` already established. |
| D-DS-2 | **`/destroy pfp` crashes for anyone without a public profile photo** — `['photos'][0]` on an empty list (`:382`), which dies in the global traceback handler with no reply at all. | **fix** — answers `battle_no_picture`, v1's own "you need a profile picture (or it's private)" string, already reused for this exact case by `fun_battle`'s port. No new string invented. |
| D-DS-3 | **The `pfp` path builds a Telegram file URL carrying the bot token** and fetches it with `requests` (`:383`) — the same construction `x_reverse_search` removed as a credential leak (D-RS-1). Here it is only sent to Telegram itself, so it is not a third-party leak, but it is the same pattern and it is unnecessary. | **fix** — nothing constructs that URL. The `file_id` crosses the queue and the worker downloads through the authenticated Bot API session. |
| — | ffmpeg is invoked with no timeout. | **fix** — `FFMPEG_TIMEOUT_SECONDS = 60`, a v2-only addition. |

## Preserved deliberately

- **Video and GIF distortion stay disabled.** `destroy.video` and
  `destroy.gif` are v1's own strings for exactly this, and the frame pipeline
  behind them (`Distortioner.py:14-98`: `TicketedDict`, ten frame workers,
  three ffmpeg re-encodes) is unreachable from the bot — every call site that
  would enter it answers one of those two strings first. It is not ported.
- **`endswith('pfp')` against the whole message**, so `/destroy my pfp` still
  matches, exactly as in v1.
- **`sendAudio` for a voice note**, not `sendVoice` — v1 uses `sendAudio` in
  both branches (`:413`).
- **25% on all three image call sites**, and `vibrato=f=10:d=1` on both audio
  ones — v1 never varies either constant.
- **PNG for a sticker, JPEG for a photo** — v1's `distorted.png` vs
  `distorted.jpg`.

## Substitution: seam carving instead of ImageMagick

`liquid_rescale` is ImageMagick's content-aware resize, reached in v1 through
`wand`. Reproducing it that way needs a **liblqr-enabled ImageMagick in the
runtime image** plus its Python binding — a system dependency the wolfi-based
worker image does not carry and cannot be verified from the wheel.
`cb_worker/distort.py:seam_carve` is the same algorithm (Avidan & Shamir:
repeatedly remove the minimum-energy connected seam) over numpy, which is
C-backed and already the natural companion to the Pillow this package uses for
`util_birthday`'s collage.

Two consequences, both deliberate and both tested:

1. **Exact pixels differ from v1's.** The energy function and tie-breaking are
   not ImageMagick's. Nothing in v1 or QA treats the output pixels as a
   contract; the observable behaviour is "the picture comes back the same size
   and visibly mangled", which `test_distort.py` asserts directly (same size,
   materially different pixels, and *not* equal to a plain resize).
2. **The carve runs on a bounded copy** (`MAX_CARVE_DIMENSION = 256`) and the
   result is scaled back to the original size. v1 carved at full resolution and
   then threw that resolution away with the same final resize, so the visible
   result is the same distortion at the same output size — for a 1280px photo
   this is the difference between ~0.4s and tens of seconds of CPU.

## Phase 6 — parity

| Behaviour | v1 | v2 | Same? |
|---|---|---|---|
| Triggers and gate | 3 spellings, `functionsFun` | same | ✅ |
| No reply / unknown reply | `destroy.instru` | same | ✅ |
| Video / animation / animated sticker | `destroy.video` / `destroy.gif` | same | ✅ |
| Photo | largest size, carve 25%, `sendPhoto` reply | same, from the worker | ⚠️ timing, D-DS-1 |
| Audio and voice | vibrato 10/1, `sendAudio` reply | same, from the worker | ⚠️ timing, D-DS-1 |
| Static sticker | carve 25% to PNG, `sendSticker` reply | same, from the worker | ⚠️ timing, D-DS-1 |
| `pfp`, photo present | distorts the caller's avatar | same | ✅ |
| `pfp`, no photo | crash, no reply | `battle_no_picture` | ⚠️ D-DS-2 |
| Output pixels | ImageMagick's seams | numpy's seams | ⚠️ by substitution |
| Concurrency | one at a time, busy-wait | semaphore, default 2 | ⚠️ D3 |

## Tests

| Layer | File |
|---|---|
| Unit (pixels/samples) | `packages/cb-worker/tests/test_distort.py` — carve arithmetic, "not a plain resize", RGBA input, the size bound, the ffmpeg arm (skips without ffmpeg) |
| Unit (job) | `packages/cb-worker/tests/test_distortion_job.py` — per-kind send call and filename, download failure, distortion failure, and the concurrency bound |
| Unit (trigger) | `packages/cb-gateway/tests/test_destroy.py` — aliases and the reply-resolution table |
| Acceptance | `qa/features/x_distortion.feature` + `qa/test_x_distortion.py` — twelve scenarios, authored |
