# x_speech_to_text — Tasks

Grammar: `tlc-spec-driven` §5. Read `spec.md` and `design.md` first, and
`.specs/features/x_conversational_ai/design.md` alongside them — this feature
ships in the same slice and its T3 depends on that feature's T6
(`chat_ai.reply_with_ai`).

Two shapes, both settled by the owner on 2026-08-03: **(a)** the ported
voice→AI sub-step, and **(b)** a net-new standalone transcript command.

## Status

| Task | Status | Notes |
|------|--------|-------|
| T1 — Harden `LLMRouter.transcribe` | ✅ done | timeout, breaker, usage row, budget |
| T2 [P] — Strings, settings, aliases | ✅ done | `cb.json` only — never `lib.json` |
| T3 — Both handlers and registration | ✅ done | depends on `x_conversational_ai` T6 |
| T4 — Unit tests | ✅ done | |
| T5 — Acceptance suite | ✅ done | authored, not ported |
| T-final — Close out | ✅ done | |

## Tasks

### T1 — Harden `LLMRouter.transcribe`

- **Skills:** /implement-feature
- **What:** Per design R3. `LLMRouter.transcribe` (`llm/router.py:177-206`) is
  thinner than `complete` in four ways that all matter here. Wrap the provider
  call in `asyncio.timeout(cfg.timeout)` using the `transcribe` task's own
  `TaskConfig.timeout`, which already exists and is already defaulted — v1 sets
  **no** timeout on this call while setting `timeout=10` on its sibling chat
  call (`../COOKIEBOT-Telegram-Group-Bot/Bot/Audio.py:26-30` vs
  `Bot/NaturalLanguage.py:34`), so a hung request pins a worker forever
  (D-ST-2). Add `user_id: int | None = None` and persist an `llm_usage` row the
  same way `complete` does, so transcription spend stops being invisible. Add
  `tenant_id: str | None = None` and run the same `ensure_within_budget` check.
  Give it the per-provider breaker from `LLMRouter._breakers` that `complete`
  already uses. **`Transcript` carries no `cost_usd`** — whisper is not priced
  in `catalog.py` and per HANDOFF §6.3 no number is to be guessed — so the row
  records tokens and latency with a null cost. Say that in the contract rather
  than inventing a price.
- **Where:** `packages/cb-core/src/cb_core/llm/router.py`,
  `packages/cb-core/tests/test_llm.py`
- **Depends on:** `x_conversational_ai` T2 (for `ensure_within_budget` and
  `LLMBudgetExceededError`)
- **Reuses:** `LLMRouter.complete` (`router.py:100-175`) is the worked example
  for all four — the breaker check, the `_meter` call, the `_persist` call and
  the exception mapping. Change nothing about `complete`.
- **Done when:** a slow provider raises rather than hanging; a usage row is
  written when `group_id` is set; an over-budget tenant is refused before the
  provider is called; an open breaker short-circuits; and every existing
  `test_llm.py` test still passes.
- **Gate:** `uv run pytest packages/cb-core/tests/test_llm.py -q`
- **Commit:** `feat(llm): bound and meter transcription the way completion already is`
- **→ R3.1–R3.4**

### T2 [P] — Strings, settings, aliases

- **Skills:** /implement-feature
- **What:** Per design R2.1, R4 and R5. Three keys in
  `locale_data/{en,pt,es}/cb.json` — `transcribe_no_voice`,
  `transcribe_too_long` (carries a `%(max)s` placeholder), `transcribe_failed`.
  All are authored: v1 has no i18n on this path at all — neither `Audio.py` nor
  `NaturalLanguage.py` imports the locale module, so there is nothing to copy.
  One setting in `cb_core/settings.py` and `.env.example`:
  `transcribe_max_duration_seconds: int = 300`. **Do not add a transcription
  timeout setting** — that number is `DEFAULT_TASKS["transcribe"].timeout`,
  already overridable through `CB_LLM_TASKS`, and a second knob for the same
  value is a trap. Add `/transcribe` with the aliases `/transcrever` (pt) and
  `/transcribir` (es) to `COMMAND_ALIASES` in `cb_core/textmatch.py`; grep all
  three spellings first to confirm none collides.
- **Where:** `packages/cb-core/src/cb_core/locale_data/{en,pt,es}/cb.json`,
  `packages/cb-core/src/cb_core/settings.py`, `.env.example`,
  `packages/cb-core/src/cb_core/textmatch.py`
- **Depends on:** none
- **Reuses:** `cb.json` is the v2-only overlay layered over `lib.json`
  (`locales.py:85-99`). `COMMAND_ALIASES` already carries every other
  multilingual trigger — follow its existing shape exactly.
- **Done when:** all three keys resolve in all three languages, all three
  command spellings parse to the same command, and `test_locales.py` is still
  green — including `TestByteIdenticalToV1`, which diffs `lib.json` against v1
  byte-for-byte. **Touching `lib.json` breaks that test.**
- **Gate:** `uv run pytest packages/cb-core/tests/test_locales.py packages/cb-core/tests/test_textmatch.py -q`
- **Commit:** `feat(locales): the transcription strings and triggers v1 never had`
- **→ R2.1, R4, R5**

### T3 — Both handlers and registration

- **Skills:** /migrate-feature (shape a), /implement-feature (shape b)
- **What:** One new module carrying both shapes, per design R1 and R2.
  **Shape (a), the port:** `F.chat.type != ChatType.PRIVATE`, `F.voice`,
  `FeatureGate("fun")`, plus the reply-to-this-bot check — v1's trigger is
  `content_type == "voice"` and `funfunctions` and the note replying to one of
  the bot's own messages (`../COOKIEBOT-Telegram-Group-Bot/Bot/COOKIEBOT.py:155,160`).
  **No `fun_off` notice** when the gate is closed; v1 sends none on this path.
  A voice note that is not a reply to the bot must fall through untouched.
  Check `message.voice.duration` against `transcribe_max_duration_seconds`
  **before downloading anything** — the metadata is already in the update, so
  an oversized note costs neither a download nor a transcription (D-ST-3).
  Bytes come from `await bot.download(message.voice.file_id)`; **nothing is
  ever written to disk** — v1's `stt.ogg` is one fixed filename shared across a
  50-thread pool (`Bot/Audio.py:23,25`, D-ST-1), and v2's provider takes
  `bytes` directly. Pass `language=ctx.lang` (D-ST-5, v1 passes none). The
  transcript goes straight into `chat_ai.reply_with_ai(...)` as `text` and is
  **never shown** — v1 parity — with no `.capitalize()` (D-ST-4). The per-user
  consecutive counter is **not** touched on this path; the per-group window and
  the tenant budget **are**.
  **Shape (b), net-new:** the `/transcribe` command, gated on **`utility`**
  with `deny_if_disabled(message, ctx, "utility")` and its standard
  `utility_off` notice — deliberately unlike shape (a). It must be a reply to a
  message carrying a voice note; anything else replies `transcribe_no_voice`
  rather than staying silent. Same cap, same download, same call. The
  transcript is sent as a reply to the **voice note**, truncated to 4000
  characters plus `…` if longer. `typing` chat action first.
  Add `cb_gateway_transcribe_total{shape,outcome}` per R6. Register the router
  next to `chat_ai.router` in `build_router`'s "content rules" block — `F.voice`
  is disjoint from `F.text` so their relative order is irrelevant, but both
  must stay ahead of `embedder`.
- **Where:** `packages/cb-gateway/src/cb_gateway/handlers/transcribe.py` (new),
  `packages/cb-gateway/src/cb_gateway/handlers/__init__.py` (one import, one
  `include_router`)
- **Depends on:** T1, T2, and `x_conversational_ai` T6
- **Reuses:** `handlers/fun_random.py:177-186`'s `_download` for the exact
  `bot.download(file_id)` idiom; `chat_ai.reply_with_ai` for the whole AI half
  of shape (a) — do not reimplement any of it; `context.py`'s `deny_if_disabled`
  for shape (b)'s gate; `filters.py`'s `FeatureGate` for shape (a)'s.
- **Done when:** a voice note replying to the bot produces an AI reply and no
  transcript message; a voice note that is not a reply to the bot is ignored;
  an over-length note is refused before any download; `/transcribe` on a voice
  reply answers with the transcript next to the audio; `/transcribe` on
  anything else says so; no test writes a file to disk.
- **Gate:** `uv run mypy packages/cb-gateway/src/cb_gateway/handlers/transcribe.py`
- **Commit:** `feat(x_speech_to_text): the voice half of the AI, and a transcript command v1 never had`
- **→ R1, R2, R6**

### T4 — Unit tests

- **Skills:** /implement-feature
- **What:** Cover both shapes with a fake bot and a fake router. Pin: the
  duration cap rejecting **before** `bot.download` is called (assert the
  download mock was never awaited); **no filesystem write anywhere in the
  path** — the explicit regression test for v1's D-ST-1; the language hint
  reaching `transcribe`; the transcript never being sent on shape (a); the
  4000-character truncation on shape (b); `transcribe_no_voice` when the reply
  target has no voice; `utility` off producing the notice on shape (b) while
  `fun` off produces **silence** on shape (a); and every error path producing a
  visible reply (D-ST-6).
- **Where:** `packages/cb-gateway/tests/test_transcribe.py` (new)
- **Depends on:** T3
- **Reuses:** `packages/cb-gateway/tests/test_fun_random.py` for the
  fake-`bot.download` pattern.
- **Done when:** every assertion above holds.
- **Gate:** `uv run pytest packages/cb-gateway/tests/test_transcribe.py -q`
- **Commit:** `test(x_speech_to_text): the cap, the language hint and the file v2 never writes`
- **→ R1, R2**

### T5 — Acceptance suite

- **Skills:** /implement-feature
- **What:** Authored, not ported — **no scenario exists in either repo**. The
  only voice-adjacent QA file is `core_musicdetection.feature`, which covers
  Shazam (`Bot/Audio.py:6-20`), a different function in the same v1 file, and
  is out of scope. Write `qa/features/x_speech_to_text.feature` for both
  shapes: a voice note replying to the bot gets an AI reply and no transcript
  message; a voice note that is not a reply to the bot gets nothing;
  `/transcribe` on a voice reply returns the transcript; `/transcribe` with no
  voice reply explains itself; an over-length note is refused.
- **Where:** `qa/features/x_speech_to_text.feature` (new),
  `qa/test_x_speech_to_text.py` (new)
- **Depends on:** T3
- **Reuses:** `qa/test_util_isalive.py` for the pytest-bdd shape;
  `qa/conftest.py`'s `feed`, `make_message_update` and `next_update_id`.
  `make_message_update` has no `voice=` argument today — add one alongside its
  existing `sticker`/`photo`/`video` parameters rather than hand-rolling a
  payload in the step file.
- **Done when:** every scenario passes and `scripts/status.py` counts them
  against this feature.
- **Gate:** `uv run pytest qa/test_x_speech_to_text.py -q`
- **Commit:** `test(x_speech_to_text): the acceptance bar for both shapes`
- **→ spec.md "Shape (b)"**

### T-final — Close out

- **Skills:** /review-changes, /lint-code
- **What:** The §6 ritual. `docs/contracts/x_speech_to_text.md` with the
  Phase-2 behaviour table and a Phase-6 parity table naming the fixes
  (D-ST-1 through D-ST-6) and the one net-new shape. Flip `x_speech_to_text`
  to `Status.DONE` in `scripts/spec.py`, then `cb.py docs-sync`. Record in
  `docs/site/content/docs/feature-map.mdx` that shape (b) is net-new with no v1
  equivalent, the way `/trex` is recorded for `fun_partneredcons`. Update
  `HANDOFF.md` §4.
- **Where:** `docs/contracts/x_speech_to_text.md` (new), `scripts/spec.py`,
  `docs/site/content/docs/feature-map.mdx`, `HANDOFF.md`
- **Depends on:** T1–T5
- **Gate:** `uv run python scripts/cb.py check`
- **Commit:** `docs(x_speech_to_text): close out`
- **→ tlc-spec-driven §6**
