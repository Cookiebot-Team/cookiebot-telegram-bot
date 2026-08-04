# x_conversational_ai — Tasks

Grammar: `tlc-spec-driven` §5. Read `spec.md` and `design.md` first — every
behavioural decision is settled there, including the four the owner answered on
2026-08-03. An implementer should never need to open v1 to find out what a
string says; if one does, that is a bug in this file.

`.specs/features/x_speech_to_text/tasks.md` ships in the same slice and its T3
depends on T6 here.

## Status

| Task | Status | Notes |
|------|--------|-------|
| T1 — Langchain provider behind the router | ⏳ not started | |
| T2 [P] — Tenant budget cap | ⏳ not started | first thing ever to read `monthly_llm_budget_usd` |
| T3 [P] — `cache.bump_clamped` | ⏳ not started | the primitive v1's signed counter needs |
| T4 [P] — Strings and settings | ⏳ not started | `cb.json` only — never `lib.json` |
| T5 [P] — Failing unit tests for the pure logic | ⏳ not started | |
| T6 — Handler and router registration | ⏳ not started | depends on T1–T5 |
| T7 — Acceptance suite | ⏳ not started | authored, not ported — no v1 scenario exists |
| T-final — Close out | ⏳ not started | |

## Tasks

### T1 — Langchain provider behind the router

- **Skills:** /implement-feature
- **What:** Per design R1. New `LangchainProvider` implementing the
  `LLMProvider` protocol in full (`llm/base.py:12-65`), `name == "langchain"`,
  resolving fully-qualified `"provider:model"` strings through
  `langchain.chat_models.init_chat_model` and caching the resolved client per
  `(model, max_tokens, temperature, timeout)`. `complete()` maps
  `cb_core.llm.types.Message` → langchain message objects (`SystemMessage` for
  the `system` argument, `HumanMessage` for `"user"`, `AIMessage` for
  `"assistant"`), calls `ainvoke`, and builds a `Completion` with tokens from
  `response.usage_metadata` and cost from the existing `catalog.py` lookup on
  the model id **with the provider prefix stripped**. `stream()` via `astream`,
  `count_tokens()` via the client's `get_num_tokens_from_messages`.
  `transcribe()` raises
  `LLMError("langchain provider does not offer speech-to-text; route to openai")`
  — copy the shape of `anthropic_provider.py:227-230`. `close()` is a no-op.
  Register it unconditionally in `build_router` (`router.py:286-311`) and move
  **only** `DEFAULT_TASKS["chat"]` to
  `TaskConfig(provider="langchain", model="anthropic:claude-opus-5",
  max_tokens=1024, temperature=1.0, timeout=30.0)` — `moderate`, `summarize`,
  `vision` and `transcribe` do not move, so `doomlist`'s live `moderate` calls
  are untouched. Add `langchain`, `langchain-anthropic`, `langchain-openai` to
  `packages/cb-core/pyproject.toml`.
- **Where:** `packages/cb-core/src/cb_core/llm/langchain_provider.py` (new),
  `packages/cb-core/src/cb_core/llm/router.py`,
  `packages/cb-core/src/cb_core/llm/__init__.py`,
  `packages/cb-core/pyproject.toml`,
  `packages/cb-core/tests/test_llm_langchain.py` (new)
- **Depends on:** none
- **Reuses:** `anthropic_provider.py` and `openai_provider.py` as the two
  worked examples of the protocol — match their structure, their error mapping
  (`LLMRateLimitedError` / `LLMError`) and their `Completion` construction.
  `catalog.py`'s pricing lookup, unchanged. The breaker needs no work:
  `LLMRouter._breakers` is already keyed by provider name.
- **Done when:** a fake langchain client drives `complete`/`stream`/
  `count_tokens` through the provider; `transcribe` raises; the cost lookup
  resolves `"anthropic:claude-opus-5"` to the same price `"claude-opus-5"` gets
  today; an unpriced model reports `None` rather than a guess.
- **Gate:** `uv run pytest packages/cb-core/tests/test_llm_langchain.py packages/cb-core/tests/test_llm.py -q`
- **Commit:** `feat(llm): a langchain-backed provider, so a task can name any model`
- **→ R1.1–R1.10**

### T2 [P] — Tenant budget cap

- **Skills:** /implement-feature
- **What:** Per design R2. `LLMBudgetExceededError(LLMError)` in
  `llm/types.py`. New `llm/budget.py` with
  `async def month_to_date_usd(tenant_id: str) -> float` and
  `async def ensure_within_budget(tenant_id: str) -> None`. The aggregate is
  month-to-date UTC: the `llm_daily_cost` rollup for the month so far plus
  today's `llm_usage` rows (the nightly `rollup_llm_costs` has not folded those
  in yet), cached in Valkey under `cb:llm:mtd:{tenant_id}` with a 60s TTL.
  `LLMRouter.complete()` and `LLMRouter.transcribe()` each gain
  `tenant_id: str | None = None`; when set **and**
  `Tenant.monthly_llm_budget_usd is not None`, the check runs before the
  provider call. **Failure direction is not symmetric and R2.4 explains why:**
  over budget per a query that succeeded ⇒ raise; a cache or database *failure*
  ⇒ allow, log `llm.budget_check_failed`, count it. `tenant_id=None` skips the
  check entirely so no existing caller changes behaviour.
- **Where:** `packages/cb-core/src/cb_core/llm/budget.py` (new),
  `packages/cb-core/src/cb_core/llm/types.py`,
  `packages/cb-core/src/cb_core/llm/router.py`,
  `packages/cb-core/tests/test_llm_budget.py` (new)
- **Depends on:** none
- **Reuses:** `cb_core/tenancy.py`'s `registry.by_id` and the
  `monthly_llm_budget_usd` field that has existed since
  `packages/cb-api/migrations/versions/0003_tenants.py:40` and **has never been
  read by anything** — this task is the first reader. `cb_core/cache.py` for
  the TTL cache. `cb_worker/main.py`'s `rollup_llm_costs` for the shape of the
  `llm_daily_cost` table.
- **Done when:** a tenant under budget passes; one over budget raises
  `LLMBudgetExceededError` and no provider call is made; a tenant with
  `monthly_llm_budget_usd is None` is never checked; a raising cache and a
  raising database each let the call through and log.
- **Gate:** `uv run pytest packages/cb-core/tests/test_llm_budget.py packages/cb-core/tests/test_tenancy.py -q`
- **Commit:** `feat(llm): enforce the tenant budget that has been declared since 0003`
- **→ R2.1–R2.6**

### T3 [P] — `cache.bump_clamped`

- **Skills:** /implement-feature
- **What:** Per design R4.1. One new primitive in `cb_core/cache.py`:
  `async def bump_clamped(key: str, delta: int, *, lo: int, hi: int,
  initial: int, ttl_seconds: int) -> int | None` — an atomic Lua `EVAL` that
  seeds a missing key to `initial`, applies `delta`, clamps into `[lo, hi]`,
  refreshes the TTL and returns the new value. Returns `None` on any Valkey
  error, matching `incr_window`'s existing contract so callers fail open the
  same way.
- **Where:** `packages/cb-core/src/cb_core/cache.py`,
  `packages/cb-core/tests/test_cache.py`
- **Depends on:** none
- **Reuses:** `cache.incr_window` (`cache.py:115-125`) — same module, same
  client accessor, same swallow-and-return-`None` failure contract.
  **Not** `cb_core/cooldowns.py`: `TokenBucket`/`SlidingWindow`/`QuotaLedger`
  are in-process objects with no shared store, and the gateway runs replicated,
  so they cannot hold this counter (`scripts/spec.py:100` already notes the
  same gap).
- **Done when:** a missing key seeds to `initial` before the delta is applied;
  the value clamps at both ends; the TTL is refreshed on every call; a raising
  client returns `None` instead of propagating.
- **Gate:** `uv run pytest packages/cb-core/tests/test_cache.py -q`
- **Commit:** `feat(cache): a clamped counter, since a signed streak is not a window`
- **→ R4.1**

### T4 [P] — Strings and settings

- **Skills:** /implement-feature
- **What:** Per design R3.4 and R8. Three keys in
  `locale_data/{en,pt,es}/cb.json` — `ai_unavailable`, `ai_quota_spent`,
  `ai_rate_limited`. `ai_unavailable`'s **`en` value is v1's exact string**:
  `AI is temporarily unavailable. Please try again later.`
  (`../COOKIEBOT-Telegram-Group-Bot/Bot/NaturalLanguage.py:36`); v1 never
  translated it, so the `pt` and `es` values are authored here. Two settings in
  `cb_core/settings.py` and `.env.example`: `ai_chat_group_limit: int = 20`,
  `ai_chat_window_seconds: int = 60`.
- **Where:** `packages/cb-core/src/cb_core/locale_data/en/cb.json`,
  `packages/cb-core/src/cb_core/locale_data/pt/cb.json`,
  `packages/cb-core/src/cb_core/locale_data/es/cb.json`,
  `packages/cb-core/src/cb_core/settings.py`, `.env.example`
- **Depends on:** none
- **Reuses:** `cb.json` is the v2-only overlay that layers over `lib.json`
  (`locales.py:85-99`).
- **Done when:** all three keys resolve in all three languages and
  `test_locales.py` is still green — including
  `TestByteIdenticalToV1`, which diffs `lib.json` against v1 byte-for-byte.
  **Touching `lib.json` breaks that test; the overlay exists precisely so this
  task does not have to.**
- **Gate:** `uv run pytest packages/cb-core/tests/test_locales.py packages/cb-core/tests/test_settings.py -q`
- **Commit:** `feat(locales): the strings v1's AI path never had`
- **→ R3.4, R8**

### T5 [P] — Failing unit tests for the pure logic

- **Skills:** /implement-feature
- **What:** Tests written before the handler exists, per design R5.4–R5.6 and
  R6. Cover, as pure functions taking already-resolved inputs: the
  `MentionsBot` predicate (reply-to-a-bot-text ⇒ match; skin display name or
  `@username` anywhere in the text, case-insensitively ⇒ match; neither ⇒ no
  match — and **not** v1's hardcoded literals); trigger-token stripping
  (newlines become spaces, `.strip()`, and **no** `.capitalize()`, which is
  D-AI-3); the empty-after-stripping case yielding the literal `"?"` with no
  model call (v1 `NaturalLanguage.py:74`); the brevity line picked per
  language, verbatim — `en` → `Try to reduce the answer a lot.`, `pt` →
  `Tente reduzir bastante a resposta.`, `es` → `Intenta reducir mucho la
  respuesta.`, any other language → **nothing appended at all** (v1 appends a
  stray `"\n\n"`; that is dropped); and message assembly putting a replied-to
  bot text in as an **`assistant`** message, never `system` — D-AI-5 is a live
  prompt-injection hole in v1 and a test must pin the fix.
- **Where:** `packages/cb-gateway/tests/test_chat_ai.py` (new)
- **Depends on:** none
- **Reuses:** `packages/cb-gateway/tests/test_ship.py`'s pattern for testing a
  pure parsing function with no Telegram and no database.
- **Done when:** the pure-logic tests pass standalone and anything needing the
  handler errors on import because `cb_gateway.handlers.chat_ai` does not exist
  yet.
- **Gate:** `uv run pytest packages/cb-gateway/tests/test_chat_ai.py -q`
  (expected: pure-logic tests pass, the rest error on import)
- **Commit:** `test(x_conversational_ai): the trigger, the stripping and the role a reply gets`
- **→ R5.4, R5.5, R5.6, R6**

### T6 — Handler and router registration

- **Skills:** /implement-feature
- **What:** Implement `chat_ai.py` per design R5, R6 and R7. Two handlers in
  one router, **in this order**: `ai_reply`
  (`F.chat.type != ChatType.PRIVATE`, `F.text`, `FeatureGate("fun")`,
  `MentionsBot`) and `replenish` (`F.chat.type != ChatType.PRIVATE`, `F.text`,
  bumps the counter `+1` then `raise SkipHandler`). Gate order before the model
  call: per-group window (R3) → per-user counter (R4) → budget (inside the
  router, R2). The persona is R6's constant, formatted with the skin's display
  name — **not** v1's DAN text, per D-AI-1, and with no few-shot seeds. Factor
  the reply half as module-level
  `async def reply_with_ai(message, ctx, *, skin, bot_username, text) -> None`;
  `x_speech_to_text` imports exactly this. Every failure path replies
  (`ai_quota_spent` / `ai_unavailable`); the only two silences are v1's own —
  an exhausted counter and a flood above the window limit. Add
  `cb_gateway_ai_replies_total{outcome}` with the six values in R7.1.
  **Registration is load-bearing:** in `build_router`'s "content rules" block,
  after `stickerspam` and immediately **before** `embedder` — v1 runs the embed
  check only in the `else` reached when the AI branch did not match
  (`../COOKIEBOT-Telegram-Group-Bot/Bot/COOKIEBOT.py:309-316`). Reorder nothing
  else: every branch that intercepts ahead of the AI in v1 is already
  registered earlier in v2.
- **Where:** `packages/cb-gateway/src/cb_gateway/handlers/chat_ai.py` (new),
  `packages/cb-gateway/src/cb_gateway/handlers/__init__.py` (one import, one
  `include_router`), `packages/cb-gateway/tests/test_chat_ai.py` (extend)
- **Depends on:** T1, T2, T3, T4, T5
- **Reuses:** `cb_gateway/context.py`'s `context_for` / `ctx.enabled` / `t`;
  `cb_gateway/filters.py`'s `FeatureGate`; `handlers/members.py:56-72` for the
  yield-don't-consume `SkipHandler` pattern; `handlers/stickerspam.py:62`'s
  `_bump` for the `incr_window` shape and its fail-open on `None`;
  `tenancy.registry.by_skin` for the tenant and the display name.
- **Done when:** every test in `test_chat_ai.py` passes, including the ones
  T5 left failing; a mention gets a reply; `fun` off produces **no** reply and
  **no** `fun_off` notice while still replenishing the counter; the seventh
  consecutive trigger is answered with nothing at all.
- **Gate:** `uv run pytest packages/cb-gateway/tests/test_chat_ai.py -q && uv run mypy packages/cb-gateway/src/cb_gateway/handlers/chat_ai.py`
- **Commit:** `feat(x_conversational_ai): the mention trigger, without v1's jailbreak`
- **→ R5, R6, R7**

### T7 — Acceptance suite

- **Skills:** /implement-feature
- **What:** Authored, not ported — **no scenario exists in either repo**
  (`../Cookiebot-QA/features/` has none for conversational AI; confirmed
  against the full listing). Write
  `qa/features/x_conversational_ai.feature` against `spec.md`'s QA section: a
  mention triggers a reply; a reply to a bot message triggers a reply; `fun`
  off is **silent** (not a `fun_off` notice); an empty stripped message answers
  `"?"` with no model call; a branch that intercepts earlier wins over the AI
  branch; the per-user counter silences the bot after seven consecutive
  triggers and an ordinary message replenishes it.
- **Where:** `qa/features/x_conversational_ai.feature` (new),
  `qa/test_x_conversational_ai.py` (new)
- **Depends on:** T6
- **Reuses:** `qa/test_util_isalive.py` as the smallest complete example of the
  pytest-bdd shape; `qa/conftest.py`'s `feed`, `make_message_update`,
  `next_update_id` and the `telegram` fake's `calls_to`. **Take every update id
  from `next_update_id()`** — the dedupe middleware is real and a reused id is
  dropped as a redelivery, which reads exactly like "the bot said nothing".
- **Done when:** every scenario passes and `scripts/status.py` counts them
  against this feature.
- **Gate:** `uv run pytest qa/test_x_conversational_ai.py -q`
- **Commit:** `test(x_conversational_ai): the acceptance bar v1 never had`
- **→ spec.md "QA — authored, not ported"**

### T-final — Close out

- **Skills:** /review-changes, /lint-code
- **What:** The §6 ritual. `docs/contracts/x_conversational_ai.md` with the
  Phase-2 behaviour table and a Phase-6 parity table naming every recorded
  behavioural change — D-AI-1 (the DAN prompt is not ported), D-AI-6 (the
  simsimi branch is dropped), D-AI-7 (the trigger is per-skin, not hardcoded)
  and D-AI-8's additive limits. Flip `x_conversational_ai` to `Status.DONE` in
  `scripts/spec.py`, then `cb.py docs-sync`. Record the QA/v1 notes in
  `docs/site/content/docs/feature-map.mdx`. Update `HANDOFF.md` §4's next batch
  and tick §1's gaps that this closes.
- **Where:** `docs/contracts/x_conversational_ai.md` (new), `scripts/spec.py`,
  `docs/site/content/docs/feature-map.mdx`, `HANDOFF.md`
- **Depends on:** T1–T7
- **Gate:** `uv run python scripts/cb.py check`
- **Commit:** `docs(x_conversational_ai): close out`
- **→ tlc-spec-driven §6**
