# core_musicdetection — Specify

**Feature id:** `core_musicdetection` · **Area:** core · **Milestone:** M3 ·
**Kind:** v1 port with no QA scenario.

## Goal

A voice note containing music gets a reply naming the song and the artist.
Passive: no command, no argument — v1 fingerprints every voice note in a group
with utility functions on.

## Source of truth

`../COOKIEBOT-Telegram-Group-Bot/Bot/Audio.py:6-20`, dispatched from the
`voice` content-type branch at `COOKIEBOT.py:155-159`. Full behaviour table:
`docs/contracts/core_musicdetection.md` §Phase 2.

## The finding that shapes the port

`scripts/spec.py`'s row says "ShazamAPI is unofficial - feature-flag it behind
a breaker". Both halves turned out to understate the problem:

* `ShazamAPI` is unmaintained; its successor `shazamio` has a Rust core.
* **`import shazamio_core` segfaults on this workspace's Python** (3.14;
  workspace requires ≥3.13), and its `pydub` dependency needs the `audioop`
  module removed in 3.13. A segfault cannot be contained by `try/except
  ImportError`, so not even an optional extra is safe.

The port therefore keeps v1's behaviour and changes the vendor to AudD's
documented HTTP API — no new dependency, since `httpx` is already the one
outbound client (AGENTS.md §5). `set_recogniser` remains the plug point for a
deployment with a working Shazam binding.

## Decisions

| # | Decision | Why |
|---|---|---|
| R1 | The lookup is a `cb-worker` job behind `cb_core.breaker` | AGENTS.md §2.4, and this is the highest-volume outbound call in the bot: an outage without a breaker means one doomed call per voice note, forever. |
| R2 | Two independent off switches (`CB_MUSIC_DETECTION_ENABLED`, empty `CB_AUDD_API_KEY`) | It sends user audio to a third party. A deployment opts in. |
| R3 | AudD instead of Shazam | See the finding. Same trade `util_postforwarder` made for translation. |
| R4 | The handler raises `SkipHandler` on every path and is registered ahead of `transcribe` | v1 runs both the music check and the transcribe→AI sub-step for the same note. Consuming the update here would silently disable `x_speech_to_text` shape (a). |
| R5 | The `['pt', 'es']` answer split is preserved, not fixed | A Spanish group is answered in Portuguese today. There is no catalog key; inventing copy is not a port. Asserted by a test so it is not "fixed" by accident. |
| R6 | Silence on every failure path | v1's own no-match branch is silent, and a group must never learn about a deployment gap. |

## Success criteria

1. A recognised track produces v1's exact reply string, HTML, as a reply.
2. Every other outcome is silent.
3. The breaker opens after repeated failures and stops spending requests.
4. A voice note still reaches the handlers registered after this one.
5. `ruff`, `mypy` and `cb.py check` clean.
