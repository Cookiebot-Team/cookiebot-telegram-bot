# Contract: x_speech_to_text (v1 -> v2)

Phase 2/6 of `/migrate-feature` (shape (a)) plus `/implement-feature` (shape
(b), net-new) for voice transcription. QA: authored locally,
`qa/features/x_speech_to_text.feature` — no scenario exists in
`../Cookiebot-QA/features/` for either shape; the only voice-adjacent QA
file is `core_musicdetection.feature` (Shazam, a different function in the
same v1 file, out of scope). FEATURE-MAP row: `x_speech_to_text`.
Spec/design: `.specs/features/x_speech_to_text/{spec,design,tasks}.md` —
read those for the full reasoning; this file is the durable behaviour
record. Ships in the same slice as `x_conversational_ai` (`docs/contracts/
x_conversational_ai.md`); shape (a) below calls that feature's
`reply_with_ai` directly.

Files owned by this port: `packages/cb-core/src/cb_core/llm/router.py`
(edit — `transcribe`'s timeout/breaker/usage row/budget), `packages/cb-core/
src/cb_core/locale_data/{en,pt,es}/cb.json` (edit), `packages/cb-core/src/
cb_core/textmatch.py` (edit — `COMMAND_ALIASES`), `packages/cb-core/src/
cb_core/settings.py`, `.env.example` (edit), `packages/cb-gateway/src/
cb_gateway/handlers/transcribe.py` (new), `packages/cb-gateway/src/
cb_gateway/handlers/__init__.py` (one router line), and the tests listed
below.

## Phase 1 — where v1 lives

- Handler: `speech_to_text`, `../COOKIEBOT-Telegram-Group-Bot/Bot/
  Audio.py:22-32`.
- Dispatch: `COOKIEBOT.py:155-162` — the only call site, inside
  `x_conversational_ai`'s voice branch. There is no other trigger anywhere
  in v1; shape (b) below has no v1 origin at all.

## Phase 2 — v1 behaviour contract (shape (a) only — shape (b) is net-new, see below)

| Aspect | v1 behaviour (file:line) |
|---|---|
| Trigger | Not a command. `content_type == "voice"` **and** `funfunctions` **and** the voice message is a reply to a message the bot itself sent — `COOKIEBOT.py:155,160`. |
| Preconditions | The outer gate is `if utilityfunctions or funfunctions:` (`:156`), but the inner branch requires `funfunctions` (`:160`). Group/supergroup only. No admin check. No quota — the voice path never touches `remaining_responses_ai`. |
| Sibling branch | Under the same outer gate, `utilityfunctions` independently triggers `identify_music` (`core_musicdetection`, out of scope here) for the same voice message. |
| Audio acquisition | `getFile` → an HTTP download → `r.content`, held in memory, then written to a fixed local filename (`stt.ogg`). No size or duration cap anywhere. |
| Transcription | OpenAI `whisper-1`, `response_format="text"`, no `language` hint, no timeout. |
| Post-processing | `.capitalize()`. |
| Success output | Nothing is sent. The transcript is assigned to `msg['text']` and handed straight to `conversational_ai`; the user sees only the AI reply. |
| Failure output | None — no `try`/`except` at all; any failure escapes silently. |
| Persistence | None (but see D-ST-1 — the file written to disk is never deleted). |
| External calls | Telegram `getFile` + file download, OpenAI transcriptions. |

## Defects — verdict per item

| id | Defect (v1 file:line) | Verdict |
|---|---|---|
| D-ST-1 | `with open('stt.ogg', 'wb')` — one fixed filename in the process CWD, no per-request uniqueness, no lock, under a 50-worker pool. Concurrent voice notes read each other's bytes; the file is never deleted. | **fixed by construction.** v2 passes `bytes` straight from `bot.download()` to `router().transcribe()` and never writes a file — `openai_provider.py` already takes raw bytes. A unit test asserts no filesystem write anywhere in the path. |
| D-ST-2 | No timeout on the Whisper call, while the sibling chat call sets `timeout=10`. A hung request pins a worker indefinitely. | **fix.** `LLMRouter.transcribe` now wraps the provider call in `asyncio.timeout(cfg.timeout)`, using the `transcribe` task's own already-defaulted `TaskConfig.timeout`. |
| D-ST-3 | No size or duration cap between download and upload. An arbitrarily long voice note is buffered whole and billed whole. | **fix.** `message.voice.duration` is checked against `transcribe_max_duration_seconds` **before any download** — the metadata is already in the update, so an oversized note costs neither a download nor a transcription. Over the cap, the user is told (`transcribe_too_long`), not silently ignored. |
| D-ST-4 | `.capitalize()` on the transcript lowercases everything after the first character. | **fix.** Same verdict as `x_conversational_ai`'s D-AI-3. |
| D-ST-5 | No `language` hint passed to Whisper, even though the group's language is in hand. | **fix.** `language=ctx.lang` is passed on every call. |
| D-ST-6 | No error handling whatsoever; a failure is invisible to the chat. | **fix.** Every failure path (a duration-cap refusal, a download failure, a transcription error) produces a user-visible reply — `transcribe_too_long`/`transcribe_failed`, or shape (a)'s inherited `ai_unavailable`/`ai_quota_spent` for failures after the transcript exists. |

## Shape (b) — the standalone `/transcribe` command, net-new

No v1 behaviour exists for this shape at all — `/implement-feature`
territory, and `qa/features/x_speech_to_text.feature`'s command scenarios
are authored directly from `spec.md`'s "Shape (b)" section. `/transcribe`
(aliased `/transcrever` pt, `/transcribir` es, in `COMMAND_ALIASES`) replies
to a voice note with its transcript; the trigger must itself be a reply to
a message carrying a voice note, or the bot says so (`transcribe_no_voice`)
rather than staying silent. Gated on **`utility`**, not `fun` — a
transcript is a utility, not a bit — and, unlike shape (a), uses
`deny_if_disabled`, producing the standard `utility_off` notice like every
other `/`-command path in v2. Same duration cap, same no-disk download,
same bounded/language-hinted call as shape (a). The transcript replies to
the **voice note**, not the command, truncated to 4000 characters plus `…`
if it exceeds Telegram's 4096-character message cap. The transcript is
never stored, in either shape.

## Router hardening — `LLMRouter.transcribe` catches up to `complete`

Four gaps `transcribe` had that `complete` didn't, closed in this slice:

1. **Timeout (D-ST-2)** — bounded by `cfg.timeout`, the `transcribe` task's
   own `TaskConfig.timeout`, already defaulted and already overridable
   through `CB_LLM_TASKS`. A timeout surfaces as `LLMError`, and the
   breaker records the failure.
2. **Usage row** — `user_id` and `tenant_id` parameters were added;
   transcription now writes an `llm_usage` row the same way `complete`
   does. **`Transcript` carries no `cost_usd`**: whisper has no entry in
   `catalog.py`, and per HANDOFF §6.3 no price is to be guessed, so the row
   records real tokens-zero/latency/attribution with a **null** `cost_usd`
   rather than an invented number.
3. **Tenant budget** — the same `ensure_within_budget` check `complete`
   gets, ahead of the provider call.
4. **Breaker** — `transcribe` now goes through the same per-provider
   `_breakers` dict `complete` already uses, keyed by `cfg.provider`
   (`"openai"`).

The langchain provider introduced alongside this feature (see
`docs/contracts/x_conversational_ai.md`) deliberately does **not** carry
transcription: langchain has no portable speech-to-text interface across
its integrations, so `LangchainProvider.transcribe()` raises and
`DEFAULT_TASKS["transcribe"]` stays on the hand-rolled OpenAI provider,
unmoved by this slice.

## Handler shape and registration

`packages/cb-gateway/src/cb_gateway/handlers/transcribe.py` carries both
shapes in one module. **Shape (a)**, `voice_ai`: `F.chat.type !=
ChatType.PRIVATE`, `F.voice`, `FeatureGate("fun")`, plus a reply-to-this-bot
filter — a voice note that is not a reply to the bot falls through
untouched (v1 hands it only to `identify_music`). No `fun_off` notice when
the gate is closed, matching v1's own silence on this path. The per-group
AI-reply rate limit (`x_conversational_ai`'s R3) applies here too, sharing
the **same** Valkey key (`cb:ai:{group_id}`) `chat_ai.py` uses — a separate
counter would let a group double its effective AI-reply rate by
alternating text mentions and voice replies. The per-user streak is **not**
touched on this path, v1 parity: `reply_with_ai` itself never spends it.
**Shape (b)**, `transcribe_command`: the `/transcribe` trigger, gated on
`utility` with the standard notice.

Both shapes share `_get_transcript`, which enforces the duration cap before
any download, downloads via `bot.download(file_id)` (the same idiom
`fun_random.py`'s `_download` uses — an in-memory buffer, nothing ever
touches disk), and calls `router().transcribe(...)` with the language hint
and full attribution (`group_id`, `user_id`, `tenant_id`).

**Registration**: `transcribe.router` sits next to `chat_ai.router` in
`build_router`'s "content rules" block — `F.voice` is disjoint from `F.text`
so their relative order is irrelevant, but both must stay ahead of
`embedder`.

## Phase 6 — parity table

| Aspect | Verdict |
|---|---|
| Trigger (voice reply to the bot, `funfunctions`/`fun` gate, silent when off) | **same** |
| No admin check, no per-user quota on this path | **same** |
| Audio acquisition (in-memory, no disk write) | **changed (fix, D-ST-1)** — `bot.download()` → `bytes`, never a file; already impossible in v1's shape given v2's provider takes bytes directly |
| Transcription timeout | **changed (intentional, fix, D-ST-2)** — v1 had none; v2 bounds it via `cfg.timeout` |
| Duration cap | **changed (intentional, fix, D-ST-3)** — v1 had none; v2 checks `message.voice.duration` before any download, replying `transcribe_too_long` over the cap |
| `.capitalize()` on the transcript | **dropped (fix, D-ST-4)** |
| Language hint | **changed (intentional, fix, D-ST-5)** — `language=ctx.lang` now passed |
| Failure handling | **changed (intentional, fix, D-ST-6)** — every failure path replies |
| Transcript never shown, fed straight to the AI reply | **same** |
| Standalone `/transcribe` command | **net-new, no v1 equivalent** — shape (b), gated on `utility` with the standard `utility_off` notice (deliberately unlike shape (a)'s silence), truncated at 4000 chars, replies to the voice note |
| Cost/quota on the voice path | **changed (additive)** — the per-group AI rate limit and the tenant budget cap now apply, neither of which existed in v1 |

## QA

No v1 or `Cookiebot-QA` scenario exists for either shape (confirmed:
`core_musicdetection.feature` is the only voice-adjacent scenario, and it
covers Shazam, a different function in the same v1 file). `qa/features/
x_speech_to_text.feature` is authored directly from `spec.md`'s Phase 2
table (shape (a)) and its "Shape (b)" section (shape (b)). Five scenarios:
a voice note replying to the bot gets an AI reply and no transcript
message; a voice note that is not a reply to the bot gets nothing;
`/transcribe` on a voice reply returns the transcript; `/transcribe` with
no voice reply explains itself; an over-length voice note is refused
before any transcript is generated.

## Tests

| Layer | File |
|---|---|
| Unit — router hardening (timeout, breaker, usage row, budget) | `packages/cb-core/tests/test_llm.py` |
| Unit — locales/settings/aliases | `packages/cb-core/tests/test_locales.py`, `test_settings.py`, `test_textmatch.py` |
| Unit — both shapes: duration cap short-circuiting before download, no filesystem write, language hint, transcript never sent (shape a), 4000-char truncation (shape b), `transcribe_no_voice`, `fun` silence vs. `utility_off` notice, every error path replying | `packages/cb-gateway/tests/test_transcribe.py` |
| Acceptance — the five scenarios above, against the real dispatcher, a stubbed LLM router (both `.transcribe()` and, for shape (a), `chat_ai`'s own `.complete()`) and a faked `Bot.download` | `qa/features/x_speech_to_text.feature`, `qa/test_x_speech_to_text.py` |

**A real Postgres is needed by every scenario that sends a voice note**,
for a reason unrelated to this feature: `mediarestrict.enforce_media_
restriction` is registered on `_RESTRICTED_CONTENT` (which includes
`F.voice`) and sits ahead of `transcribe.router` in `build_router`'s
join-chain section. With `media_restrict_seconds` at its v1-matching
default (600, on for the QA group), it runs a `db.fetchrow` lookup over
*every* voice note, none of which is about media restriction. With no live
pool that call raises instead of returning "no `group_members` row",
crashing the scenario rather than the fail-open `SkipHandler` it intends
for an unknown join time. The `_database` fixture exists purely to give
that filter something to query "no row" against, and skips the whole file
cleanly when no database is reachable.
