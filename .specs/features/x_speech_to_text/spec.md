# x_speech_to_text — Specify

**Feature id:** `x_speech_to_text` · **Milestone:** M3 · **Kind:** state report
**Status:** `partial` — the generic transcription plumbing is built and
tested; nothing calls it, and unlike most `partial`s here the missing piece
starts with a scope decision, not just an implementation.

This is not a build spec. It records what exists, what doesn't, and why.

## What is actually implemented today

- `cb_core.llm.router().transcribe()` — routes the `transcribe` task to
  OpenAI Whisper, metered and traced identically to `complete()` —
  `packages/cb-core/src/cb_core/llm/router.py:177-204`. Default config:
  `TaskConfig(provider="openai", model="whisper-1", max_tokens=0)`
  (`router.py:65`).
- The OpenAI provider takes raw `bytes` and never touches disk —
  `packages/cb-core/src/cb_core/llm/openai_provider.py:171-186`
  (`file=(filename, audio)`, passed straight to the SDK). This matters
  because it already avoids the v1 defect below without anyone having had to
  think about it.

That is the entire footprint, same shape as `x_conversational_ai`: the
router exists, nothing outside `cb_core/llm/` references this feature.

## What is missing

- **No handler.** Same grep result as `x_conversational_ai`: nothing in
  `cb_gateway/handlers/` or `cb_worker/jobs/` calls
  `router().transcribe()`.
- **No acceptance coverage anywhere** — not in `../Cookiebot-QA/features/`,
  not in `qa/features/`. Same "20+ features never spec'd in QA" bucket as
  `x_conversational_ai` (`docs/site/content/docs/feature-map.mdx` §4).
- **A scope decision nobody has made, and it has to come before the
  handler.** v1's `speech_to_text` (`Audio.py:22-32`) is not a standalone
  "transcribe my voice note" command — there is no v1 code path where
  transcription is the whole feature and the text is shown back to the
  user. Its only call site is inside the conversational-AI voice flow
  (`COOKIEBOT.py:160-161`): a voice message that replies to the bot's own
  message gets transcribed, and the transcript is immediately handed to
  `conversational_ai` for a reply — the transcript itself is never sent
  anywhere. So "port `x_speech_to_text`" has two different honest meanings
  and nobody has picked one: (a) it's a sub-step of `x_conversational_ai`'s
  eventual handler, with no independent existence, or (b) it becomes a new,
  genuinely standalone v2 command v1 never had (e.g. "reply to any voice
  note and get the transcript back"). Building the handler without
  answering this first risks specifying behaviour v1 never had and calling
  it a port.
- **v1's implementation carries a defect that must not be ported.** It
  writes the downloaded audio to a fixed filename shared across every
  concurrent call: `with open('stt.ogg', 'wb') as audio_file` (`Audio.py:23`).
  This is the same failure shape as defect **D4** in `scripts/spec.py`'s
  `DEFECTS` table ("Fixed temp filenames raced across 50 threads") — a
  concurrent v1 process could read another request's partially-written or
  already-overwritten file. v2's provider already sidesteps this by taking
  `bytes` directly (see above), so there is nothing to fix here so much as
  something not to reintroduce.

## Why it stopped there

Same shared-plumbing-first order as `x_conversational_ai`: `router().transcribe()`
is generic infrastructure that landed without a specific caller. What makes
this one different from a plain "handler not written" gap is that the
handler that would call it is entangled with `x_conversational_ai`'s own
unbuilt handler — v1 never used transcription independently, so there is no
v1 behaviour to port for a standalone command, only a decision to make about
whether v2 should invent one. Nobody has made that decision, which is a more
honest description than "blocked": nothing external prevents making it,
it's simply an open product question that hasn't been brought to anyone.

## What it would take to finish, and what blocks it

1. **The scope decision** — sub-step of `x_conversational_ai`, or a new
   standalone command. This gates everything else and is not an engineering
   task.
2. Write the QA scenario for whichever shape is chosen — nothing exists to
   port.
3. Write the handler, using `router().transcribe()` with bytes obtained
   straight from Telegram (via aiogram's file download) and explicitly
   not v1's fixed-filename pattern.
4. If shipped as part of `x_conversational_ai`'s flow, it inherits that
   feature's own open blockers (private-chat/quota groundwork, see
   `.specs/features/x_conversational_ai/spec.md`); if standalone, it does
   not.

## v1 equivalent

`../COOKIEBOT-Telegram-Group-Bot/Bot/Audio.py:22-32` (`speech_to_text`),
called only from
`../COOKIEBOT-Telegram-Group-Bot/Bot/COOKIEBOT.py:160-161`, inside the
voice-reply branch of the conversational AI flow. (`identify_music`, the
other function in the same file, `Audio.py:6-20`, is a separate v1 feature —
tracked as `core_musicdetection`, `Status.PLANNED` — and is out of scope
here.)
