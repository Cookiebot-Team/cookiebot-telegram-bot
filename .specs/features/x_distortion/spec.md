# x_distortion — Specify

**Feature id:** `x_distortion` · **Area:** fun · **Milestone:** M3 · **Kind:**
v1 port with no QA scenario (`docs/site/content/docs/feature-map.mdx` §4).

## Goal

`/destroy` (aliased `/zoar`, `/destruir`) replies to a photo, a voice note, an
audio file or a static sticker with a mangled version of it — content-aware
squeeze for images, vibrato for audio — and to `/destroy pfp` with a mangled
copy of the caller's own profile picture.

## Source of truth

`../COOKIEBOT-Telegram-Group-Bot/Bot/Miscellaneous.py:377-433` (the branch
chain) and `Bot/Distortioner.py` (the pipeline), dispatched at
`COOKIEBOT.py:242-243`. The full behaviour table with file:line per branch is
`docs/contracts/x_distortion.md` §Phase 2.

## Two findings that shape the port

**1. Two thirds of `Distortioner.py` is unreachable.** The video arm
(`:110-143`) and everything it uses — `TicketedDict`, the ten frame workers,
three ffmpeg re-encodes, OpenCV — cannot be entered from the bot: every call
site that would reach it answers `destroy.video` or `destroy.gif` first
(`Miscellaneous.py:395-397,418-420,428-430`). v1 disabled video and GIF
distortion in the handler and left the machinery behind it in the tree. It is
not ported; the two "currently disabled" strings are.

**2. The image algorithm has no dependency-free equivalent.** `liquid_rescale`
is ImageMagick seam carving via `wand`, which would mean a liblqr-enabled
ImageMagick in the runtime image. `cb_worker/distort.py` implements the same
algorithm over numpy instead — see the contract's "Substitution" section for
what that does and does not change.

## Decisions

| # | Decision | Why |
|---|---|---|
| R1 | The gateway keeps the branch chain; download + carve + ffmpeg move to `jobs.DISTORT_MEDIA` | AGENTS.md §2.4. Every branch decision is free and synchronous and is made in v1's own order; everything after it is seconds of CPU and a subprocess. |
| R2 | Seam carving over numpy, not ImageMagick via `wand` | No system dependency, testable in the wheel, same algorithm. Pixels differ from v1's and nothing treats them as a contract. |
| R3 | The carve runs at `MAX_CARVE_DIMENSION` and is scaled back | v1 carved at full resolution and discarded it with the same final resize. Bounds one `/destroy` to ~0.4s instead of tens of seconds. |
| R4 | A real `asyncio.Semaphore` replaces the three spun-on module globals | FEATURE-MAP D3. |
| R5 | Bytes in, bytes out for images; a per-call `TemporaryDirectory` for audio | FEATURE-MAP D4 — v1's fixed filenames raced across concurrent requests. |
| R6 | `/destroy pfp` with no profile photo answers `battle_no_picture` | v1 raises and replies nothing. `fun_battle` already reuses this exact string for this exact case, so no new string is invented. |
| R7 | Nothing constructs a Telegram file URL | The `file_id` crosses the queue; the worker downloads through the authenticated session. Same rule `x_reverse_search`'s D-RS-1 established. |

## Success criteria

1. Every branch of v1's chain has an equivalent, in v1's order, answering v1's
   strings.
2. The carve produces a same-size, visibly different image that is not a plain
   resize, and survives RGBA and 1x1 inputs.
3. Concurrency is bounded by a semaphore, asserted rather than assumed.
4. `qa/features/x_distortion.feature` passes against the real dispatcher.
5. `ruff`, `mypy` and `cb.py check` clean.
