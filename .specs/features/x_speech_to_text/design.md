# x_speech_to_text — Design

Read `spec.md` first, and `.specs/features/x_conversational_ai/design.md`
alongside it — this feature's shape (a) calls that one's **R5.9**
(`reply_with_ai`), and both ship in the same slice.

## Module placement

| Piece | Where |
|---|---|
| Both handlers | `packages/cb-gateway/src/cb_gateway/handlers/transcribe.py` (new) |
| Router registration | `packages/cb-gateway/src/cb_gateway/handlers/__init__.py` (edit) |
| Command aliases | `packages/cb-core/src/cb_core/textmatch.py` (edit — `COMMAND_ALIASES`) |
| Router timeout + budget on `transcribe` | `packages/cb-core/src/cb_core/llm/router.py` (edit) |
| v2-only strings | `packages/cb-core/src/cb_core/locale_data/{en,pt,es}/cb.json` (edit) |
| Settings | `packages/cb-core/src/cb_core/settings.py`, `.env.example` (edit) |
| Unit tests | `packages/cb-gateway/tests/test_transcribe.py` (new) |
| Acceptance | `qa/features/x_speech_to_text.feature`, `qa/test_x_speech_to_text.py` (new) |

No migration, no new table, no worker job. Transcription is a single bounded
external call on the reply path — the same judgement `util_youtube` made in
reverse (it moved to the worker because its reply is not the point; here the
reply *is* the transcript, and deferring it would mean two round trips to say
one thing). If p95 latency proves otherwise, moving it behind
`cb_gateway/queue.py` is a later change that needs no schema.

## R1 — Shape (a): the ported voice → AI sub-step

- **R1.1** Handler on `F.chat.type != ChatType.PRIVATE`, `F.voice`,
  `FeatureGate("fun")`, plus the reply-to-this-bot check — v1's trigger is
  `content_type == "voice"` and `funfunctions` and the voice note replying to
  one of the bot's own messages (`COOKIEBOT.py:155,160`). A voice note that is
  not a reply to the bot is **not** this feature's; it must fall through
  untouched (v1 hands it only to `identify_music`, which is
  `core_musicdetection` and out of scope).
- **R1.2** No `fun_off` notice when the gate is closed — same reasoning as
  `x_conversational_ai`'s R5.3. Do not use `deny_if_disabled`.
- **R1.3** **The duration cap is checked first, against
  `message.voice.duration`, before anything is downloaded** (D-ST-3). The
  metadata is already in the update, so an oversized note costs neither a
  download nor a transcription. Over
  `settings.transcribe_max_duration_seconds` ⇒ reply
  `t(ctx, "transcribe_too_long", max=<seconds>)`. v1 is silent here because v1
  has no cap at all; telling the user is D-ST-6's verdict applied.
- **R1.4** Bytes come from `await bot.download(message.voice.file_id)`, the
  idiom `handlers/fun_random.py:177-186`'s `_download` already uses. **Nothing
  is ever written to disk** — that is v1's D-ST-1 race (`stt.ogg`, one fixed
  filename across 50 threads), and v2's OpenAI provider takes `bytes` directly
  (`openai_provider.py:171-192`), so the defect is impossible by construction.
  A unit test asserts no filesystem write.
- **R1.5** `await router().transcribe(audio, filename="voice.ogg",
  language=ctx.lang, group_id=ctx.group_id, user_id=..., tenant_id=...)`.
  The language hint is D-ST-5's fix — v1 passes none despite having the group's
  language in hand.
- **R1.6** The transcript is fed straight into
  `chat_ai.reply_with_ai(message, ctx, skin=..., bot_username=..., text=transcript)`
  and is **never shown to the user** — v1 parity (`COOKIEBOT.py:161-162`
  assigns it to `msg['text']` and nothing else reads it). No `.capitalize()`
  (D-ST-4).
- **R1.7** The per-user consecutive counter is **not** touched on this path —
  v1's voice branch has no `decrease_remaining_responses_ai` call. The
  per-group rate limit and the tenant budget cap **do** apply, since they are
  new v2 protections rather than ported behaviour, and an unmetered voice path
  would be a hole straight through both.
- **R1.8** Registration: next to `chat_ai.router` in `build_router`'s "content
  rules" block. `F.voice` is disjoint from `F.text`, so relative order between
  the two is irrelevant; both must stay ahead of `embedder`.

## R2 — Shape (b): the standalone transcript command (net-new)

No v1 behaviour exists. `/implement-feature` territory; the acceptance file is
authored from `spec.md`'s shape-(b) section.

- **R2.1** Trigger `/transcribe`, with aliases `/transcrever` (pt) and
  `/transcribir` (es), added to `COMMAND_ALIASES` in `cb_core/textmatch.py`
  the way every other multilingual trigger is. Grep confirms none of the three
  spellings collides with an existing command.
- **R2.2** Gated on **`utility`**, not `fun` — a transcript is a utility, not a
  bit — and unlike shape (a) it uses `deny_if_disabled(message, ctx, "utility")`,
  producing the standard `utility_off` notice like every other `/`-command in
  v2.
- **R2.3** It must be a reply to a message carrying a voice note. Anything else
  replies `t(ctx, "transcribe_no_voice")` rather than staying silent.
- **R2.4** Same cap (R1.3), same download (R1.4), same call (R1.5), same
  no-disk guarantee.
- **R2.5** The transcript is sent as a reply to the **voice note**
  (`message.reply_to_message.reply(...)`), not to the command — the transcript
  belongs next to the audio it transcribes.
- **R2.6** Telegram caps a message at 4096 characters. A longer transcript is
  truncated to 4000 plus `…`. This answers the open question `spec.md` left for
  design: one truncated message, not a thread of continuations, because the
  cap in R1.3 already bounds how long a transcript can plausibly get and a
  multi-message flood is worse than a visible cut.
- **R2.7** `typing` chat action before the call, matching every other handler
  that goes to a network.

## R3 — Router changes for `transcribe`

`LLMRouter.transcribe` (`router.py:177-206`) is thinner than `complete` and
three of the gaps matter here:

- **R3.1 (D-ST-2)** It applies no timeout. Wrap the provider call in
  `asyncio.timeout(cfg.timeout)` using the `transcribe` task's own
  `TaskConfig.timeout`, which already exists and is already defaulted. v1 sets
  no timeout at all on this call while setting one on its sibling chat call —
  `util_youtube` bounded the same class of unbounded v1 call.
- **R3.2** It accepts no `user_id` and never calls `_persist`, so transcription
  spend is invisible in `llm_usage`. Add `user_id: int | None = None` and
  persist the same way `complete` does. Note in the contract that
  `Transcript` carries no `cost_usd` today (whisper is not in `catalog.py`, and
  per HANDOFF §6.3 no number is to be guessed) — the row records tokens and
  latency with a null cost until someone supplies authoritative pricing.
- **R3.3** Add `tenant_id: str | None = None` and run the same
  `ensure_within_budget` check `complete` gets in `x_conversational_ai`'s R2.5.
- **R3.4** `transcribe` has no breaker today. Give it the same per-provider
  breaker `complete` uses — it is the same `_breakers` dict, keyed the same way.

## R4 — Settings

`CB_`-prefixed, in `cb_core/settings.py` and `.env.example`:

- `transcribe_max_duration_seconds: int = 300`
- The transcription timeout is **not** a new setting — it is
  `DEFAULT_TASKS["transcribe"].timeout`, overridable through the existing
  `CB_LLM_TASKS`. Adding a second knob for the same number would be a trap.

## R5 — Strings

`cb.json` overlay only — `lib.json` is byte-identical-tested against v1
(`test_locales.py::TestByteIdenticalToV1`). All three of
`locale_data/{en,pt,es}/cb.json`:

| Key | en |
|---|---|
| `transcribe_no_voice` | reply to a voice message to transcribe it |
| `transcribe_too_long` | that voice message is too long to transcribe (max `%(max)s` seconds) |
| `transcribe_failed` | could not transcribe that voice message |

`transcribe_failed` covers every error path in both shapes (D-ST-6): an
`LLMError`, a timeout, a failed download. Shape (a) additionally inherits
`ai_unavailable`/`ai_quota_spent` from `x_conversational_ai` for failures that
happen after the transcript exists.

## R6 — Telemetry

`cb_gateway_transcribe_total{shape,outcome}` — `shape` ∈ `voice_ai | command`,
`outcome` ∈ `ok | too_long | no_voice | error`. Eight combinations, no group or
user labels.

## R7 — Open decisions, answered

1. **Reply path or worker?** Reply path — see the placement note above.
2. **Trigger spelling and aliases?** R2.1.
3. **Transcripts over Telegram's message limit?** Truncate at 4000 + `…`
   (R2.6).
4. **Which flag gates the standalone command?** `utility`, with the standard
   notice (R2.2) — deliberately different from shape (a), which is silent
   because v1 is silent.
5. **Audio files, video notes, forwarded media?** Out. Voice notes only, as in
   v1.
