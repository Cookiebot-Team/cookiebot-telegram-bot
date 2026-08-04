# x_speech_to_text — Specify

**Feature id:** `x_speech_to_text` · **Milestone:** M3 · **Kind:** mixed — port
(the voice→AI sub-step) + net-new (a standalone transcript command)
**v1 source:** `Bot/Audio.py:22-32` (`speech_to_text`), called only from
`Bot/COOKIEBOT.py:160-161`.

This supersedes the state-report that lived here. Read
`.specs/features/x_conversational_ai/spec.md` alongside it: v1's only call site
is inside that feature's voice branch, and the two ship together.

## Goal

Turn a Telegram voice note into text. Two shapes, both settled by the owner
(2026-08-03):

- **(a) The ported sub-step.** A voice note that replies to one of the bot's
  own messages is transcribed and the transcript is fed to
  `x_conversational_ai`, exactly as v1 did. The transcript itself is never
  shown.
- **(b) A net-new standalone command.** Reply to any voice note with the
  transcription trigger and get the transcript back. v1 has no equivalent —
  this is `/implement-feature` territory, not a port, and its acceptance
  criteria are authored here.

## What already exists (do not rebuild)

- `cb_core.llm.router().transcribe()` — routes the `transcribe` task, metered
  and traced identically to `complete()`
  (`packages/cb-core/src/cb_core/llm/router.py:177-204`). Default
  `TaskConfig(provider="openai", model="whisper-1", max_tokens=0)`
  (`router.py:65`).
- `openai_provider.py:171-186` takes raw `bytes` and never touches disk
  (`file=(filename, audio)` straight to the SDK) — which is why **D-ST-1**
  below is already impossible in v2 rather than something to fix.
- Nothing calls either. No handler, no job.
- No QA scenario in either repo — `../Cookiebot-QA/features/` has
  `core_musicdetection.feature` (Shazam, a different function in the same v1
  file) and nothing for transcription.

## Phase 2 — v1 behaviour contract (shape (a) only)

| Aspect | v1 behaviour (file:line) |
|---|---|
| Trigger | Not a command. `content_type == "voice"` **and** `funfunctions` **and** the voice message is a reply to a message the bot itself sent — `COOKIEBOT.py:155,160`. |
| Preconditions | The outer gate is `if utilityfunctions or funfunctions:` (`:156`), but the inner branch requires `funfunctions` (`:160`), so `funfunctions` is the real requirement. Group/supergroup only (private chats return at `:110`). No admin check. No quota: the voice path never touches `remaining_responses_ai`. |
| Sibling branch | Under the same outer gate, `utilityfunctions` independently triggers `identify_music` (`:158-159`) — `core_musicdetection`, out of scope here, but both run for the same voice message when both flags are on. |
| Audio acquisition | `get_media_content(..., 'voice', ...)` (`:157`) → `getFile(msg['voice']['file_id'])['file_path']` (`universal_funcs.py:173`) → `requests.get(f"https://api.telegram.org/file/bot{token}/{path}", allow_redirects=True, timeout=60)` (`:176-177`) → `r.content`, held in memory. No size or duration cap anywhere. |
| Transcription | OpenAI `whisper-1`, `response_format="text"`, **no** `language` hint, **no** timeout (`Audio.py:26-30`). |
| Post-processing | `.capitalize()` (`Audio.py:31`). |
| Success output | Nothing is sent. The transcript is assigned to `msg['text']` (`COOKIEBOT.py:161`) and handed straight to `conversational_ai`; the user sees only the AI reply. |
| Failure output | None. `speech_to_text` has no `try`/`except` at all; any failure escapes to `COOKIEBOT.py:329-330`, which DMs the owner a traceback and leaves the chat silent. |
| Persistence | None (but see D-ST-1 — a file is written to disk and never deleted). |
| External calls | Telegram `getFile` + file download, OpenAI transcriptions. |

## Known defects — preserve / fix verdict

| # | Defect (v1 file:line) | Verdict |
|---|---|---|
| **D-ST-1** | `with open('stt.ogg', 'wb')` — one fixed filename in the process CWD, no per-request uniqueness, no lock, under a 50-worker pool (`Audio.py:23,25`, `COOKIEBOT.py:47`). Concurrent voice notes read each other's bytes; the file is never deleted. | **Fix — already fixed by construction.** Same class as **D4** in `scripts/spec.py`'s defects table. v2 passes `bytes` to `router().transcribe()` and never writes a file. Nothing to do beyond not reintroducing it; a test asserts no filesystem write. |
| **D-ST-2** | No timeout on the Whisper call (`Audio.py:26-30`), while the sibling chat call sets `timeout=10`. A hung request pins a worker indefinitely. | **Fix.** Bounded, like `util_youtube` bounded v1's untimed `googleapiclient` call. |
| **D-ST-3** | No size or duration cap between download and upload (`universal_funcs.py:170-183`). An arbitrarily long voice note is buffered whole and billed whole. | **Fix.** A duration cap, checked against `message.voice.duration` **before** any download — the metadata is already in the update, so an oversized note costs neither a download nor a transcription. Over the cap the user is told, rather than silently ignored. |
| **D-ST-4** | `.capitalize()` on the transcript (`Audio.py:31`) lowercases everything after the first character. | **Fix.** Same verdict as `x_conversational_ai`'s D-AI-3. |
| **D-ST-5** | No `language` hint is passed to Whisper (`Audio.py:26-30`) even though the group's language is in hand. | **Fix.** Pass the group's language. This changes recognition quality, not the shape of the output. |
| **D-ST-6** | No error handling whatsoever; a failure is invisible to the chat. | **Fix.** Every failure path produces a user-visible reply. |

## Shape (b) — the standalone command, net-new

No v1 behaviour exists, so this section **is** the spec, and
`qa/features/x_speech_to_text.feature` is authored from it.

- Reply to a voice note with the trigger; the bot replies to the **voice note**
  with its transcript.
- Gated on the same flag family as its siblings — `utilityfunctions`, because
  a transcript is a utility, not a bit — and, unlike the ported sub-step, an
  off flag produces the standard `utility_off` notice, matching every other
  `/`-command path in v2.
- Same duration cap, same bounded call, same reply-to-the-voice-note shape as
  (a).
- If the trigger is used on anything that is not a reply to a voice note, the
  bot says so rather than staying silent.
- The transcript is never stored.

Open, and answered in `design.md` rather than guessed here: the exact trigger
spelling and its aliases across `en`/`pt`/`es`, and where the reply lands when
the transcript exceeds Telegram's message limit.

## Scope

In: the voice→AI sub-step (a), the standalone command (b), the duration cap,
the langchain-backed transcription path, unit tests, an authored acceptance
suite.

Out: `identify_music`/Shazam (`core_musicdetection`, `Audio.py:6-20`);
transcription of audio files, video notes or forwarded media — voice notes
only, as in v1.

## v1 equivalent

`../COOKIEBOT-Telegram-Group-Bot/Bot/Audio.py:22-32`, called only from
`../COOKIEBOT-Telegram-Group-Bot/Bot/COOKIEBOT.py:160-161`.
