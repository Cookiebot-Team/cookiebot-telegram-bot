# x_conversational_ai — Design

Read `spec.md` first. Requirements are numbered `R<n>.<m>`; `tasks.md`
back-references them. Every path below is exact.

`x_speech_to_text`'s `design.md` covers the voice half and depends on **R5.9**
here.

## Module placement

| Piece | Where |
|---|---|
| Langchain provider | `packages/cb-core/src/cb_core/llm/langchain_provider.py` (new) |
| Budget enforcement | `packages/cb-core/src/cb_core/llm/budget.py` (new) |
| New exception | `packages/cb-core/src/cb_core/llm/types.py` (edit) |
| Router wiring | `packages/cb-core/src/cb_core/llm/router.py` (edit) |
| Clamped counter primitive | `packages/cb-core/src/cb_core/cache.py` (edit) |
| Handler | `packages/cb-gateway/src/cb_gateway/handlers/chat_ai.py` (new) |
| Router registration | `packages/cb-gateway/src/cb_gateway/handlers/__init__.py` (edit) |
| v2-only strings | `packages/cb-core/src/cb_core/locale_data/{en,pt,es}/cb.json` (edit) |
| Settings | `packages/cb-core/src/cb_core/settings.py`, `.env.example` (edit) |
| Unit tests | `packages/cb-core/tests/test_llm_langchain.py`, `packages/cb-core/tests/test_llm_budget.py`, `packages/cb-gateway/tests/test_chat_ai.py` (new) |
| Acceptance | `qa/features/x_conversational_ai.feature`, `qa/test_x_conversational_ai.py` (new) |

No migration. No new table. Nothing distributed — the only writes are the
`llm_usage` rows `LLMRouter._persist` already makes.

## R1 — The langchain provider

Owner's decision: langchain goes in **behind** the existing router, as one more
`LLMProvider`. Metering, breaker, refusal fallback, telemetry and every existing
caller are untouched; the two hand-rolled providers stay exactly as they are.

- **R1.1** `LangchainProvider` implements the `LLMProvider` protocol
  (`llm/base.py:12-65`) in full. `name` returns `"langchain"`.
- **R1.2** The model string is **fully qualified** — `"anthropic:claude-opus-5"`,
  `"openai:gpt-4o-mini"` — and resolved with
  `langchain.chat_models.init_chat_model(model, ...)`, which is the
  multi-provider/model-routing entry point the owner asked for. Instances are
  cached per `(model, max_tokens, temperature, timeout)` so a hot path does not
  re-resolve a client per call.
- **R1.3** `complete()` maps `Sequence[Message]` (`llm/types.py`) to langchain
  message objects — `SystemMessage` for the `system` argument, `HumanMessage`
  for `"user"`, `AIMessage` for `"assistant"` — calls `ainvoke`, and builds a
  `Completion` from the result: text from `response.text()`, tokens from
  `response.usage_metadata` (`input_tokens`, `output_tokens`, and
  `input_token_details.get("cache_read")` when present), `stop_reason` from
  `response.response_metadata`. Cost comes from the existing `catalog.py`
  lookup keyed on the model id **with the provider prefix stripped**, so
  `"anthropic:claude-opus-5"` prices exactly as `"claude-opus-5"` does today;
  an unpriced model reports `None`, per HANDOFF §6.3.
- **R1.4** `stream()` is `astream`, yielding each chunk's text.
  `count_tokens()` uses the resolved client's
  `get_num_tokens_from_messages`.
- **R1.5** `transcribe()` **raises** `LLMError("langchain provider does not
  offer speech-to-text; route to openai")`, mirroring
  `anthropic_provider.py:227-230` verbatim in shape. langchain has no portable
  transcription interface, so the `transcribe` task stays on the hand-rolled
  OpenAI provider. This is a deliberate limit of the decision, recorded here
  rather than worked around.
- **R1.6** `close()` is a no-op; langchain's clients hold no pool this code owns.
- **R1.7** `build_router` (`router.py:286-311`) registers
  `providers["langchain"] = LangchainProvider(settings)` unconditionally —
  credential resolution happens inside the per-model integration package, so
  there is nothing to gate on at boot. An unconfigured model surfaces as an
  `LLMError` at call time, the same way an unconfigured provider already does.
- **R1.8** `DEFAULT_TASKS["chat"]` becomes
  `TaskConfig(provider="langchain", model="anthropic:claude-opus-5",
  max_tokens=1024, temperature=1.0, timeout=30.0)`. `temperature=1.0` is v1's
  (`NaturalLanguage.py:34`). `effort` is dropped for this task — there is no
  portable effort parameter across providers, and carrying one that only works
  for a single backend defeats the point of the abstraction. **No other task
  moves**: `moderate`, `summarize`, `vision` and `transcribe` keep their current
  providers, so `doomlist`'s live `moderate` calls are not touched by this
  slice. `CB_LLM_TASKS` can move any task onto langchain later without code.
- **R1.9** The breaker needs no change: `LLMRouter._breakers` is already keyed
  by provider name, so `"langchain"` gets its own.
- **R1.10** Dependencies added to `packages/cb-core/pyproject.toml`:
  `langchain`, `langchain-anthropic`, `langchain-openai`.

## R2 — The tenant budget cap

Owner's decision: a **hard cap**, per HANDOFF §6.6 — over budget, the call is
refused and the user is told the quota is spent. `Tenant.monthly_llm_budget_usd`
has existed since `0003_tenants.py:40` and has **never been read by anything**;
this is where it starts being enforced.

- **R2.1** `LLMBudgetExceededError(LLMError)` in `llm/types.py`, alongside
  `LLMRateLimitedError` and `LLMUnavailableError`.
- **R2.2** `llm/budget.py` exposes
  `async def month_to_date_usd(tenant_id: str) -> float` and
  `async def ensure_within_budget(tenant_id: str) -> None`.
- **R2.3** The spend aggregate is month-to-date UTC: the `llm_daily_cost`
  rollup (written nightly by `cb_worker/main.py`'s `rollup_llm_costs`) for the
  month so far, plus today's `llm_usage` rows, which the rollup has not yet
  folded in. Cached in Valkey under `cb:llm:mtd:{tenant_id}` with a 60s TTL, so
  a chatty group costs one aggregate query a minute, not one per message.
- **R2.4** **Failure direction, stated explicitly.** Over budget per a query
  that succeeded ⇒ **refuse** — that is the hard cap. A cache or database
  *failure* ⇒ **allow**, log `llm.budget_check_failed`, and count it. An
  infrastructure outage is not evidence of overspend, and every other
  infra-failure path in v2 fails open (`cache.incr_window` returning `None` in
  `stickerspam._bump` is the precedent). The cap protects against spend, not
  against Postgres being down.
- **R2.5** `LLMRouter.complete()` and `LLMRouter.transcribe()` each gain
  `tenant_id: str | None = None`. When it is set **and** the tenant's
  `monthly_llm_budget_usd` is not `None`, `ensure_within_budget` runs before the
  provider call. `tenant_id=None` skips the check entirely, so no existing
  caller changes behaviour.
- **R2.6** The handler gets its tenant from `tenancy.registry.by_skin(skin)`,
  where `skin` is the kwarg the dispatcher already injects into handlers
  (`qa/conftest.py`'s `feed` passes `skin="cookiebot"`).

## R3 — Per-group rate limit

- **R3.1** `cache.incr_window(f"cb:ai:{group_id}", settings.ai_chat_window_seconds)`,
  exactly the primitive and key shape `stickerspam._bump`
  (`handlers/stickerspam.py:62`) already uses. Over
  `settings.ai_chat_group_limit` within the window, no model call is made.
- **R3.2** Mirroring stickerspam's own convention: at `count == limit` the bot
  replies `t(ctx, "ai_rate_limited")` once; above the limit it stays silent, so
  a spamming group is not answered with a wall of notices.
- **R3.3** `incr_window` returning `None` (Valkey unreachable) fails **open** —
  same as stickerspam.
- **R3.4** New settings, `CB_`-prefixed as usual: `ai_chat_group_limit: int = 20`,
  `ai_chat_window_seconds: int = 60`.

## R4 — The per-user consecutive counter (v1 parity)

v1's real quota, ported as observable behaviour. It is **not** a time cooldown:
it is a signed counter in `[-7, 7]` that every AI trigger spends and every
ordinary message replenishes (`Cooldowns.py:5,24-36`, `COOKIEBOT.py:306,313`).

- **R4.1** `cb_core.cooldowns` cannot serve this: `TokenBucket`/`SlidingWindow`/
  `QuotaLedger` are in-process objects with no shared backing store, and the
  gateway runs replicated. `cache.incr_window` cannot either — it only
  increments. So `cb_core/cache.py` gains one primitive:
  `async def bump_clamped(key: str, delta: int, *, lo: int, hi: int,
  initial: int, ttl_seconds: int) -> int | None`, an atomic Lua `EVAL` that
  seeds a missing key to `initial`, applies `delta`, clamps into `[lo, hi]`,
  refreshes the TTL and returns the new value. `None` on any Valkey error,
  matching `incr_window`'s contract.
- **R4.2** Key `cb:ai:streak:{user_id}` — **per user, not per group**, because
  v1's `remaining_responses_ai` is a process-global dict keyed only by
  `msg['from']['id']` (`Cooldowns.py:8`). TTL 86400s.
- **R4.3** Spend: `bump_clamped(delta=-1, lo=-7, hi=7, initial=7)`. The gate is
  the **post-decrement** value `> 0`, exactly v1's order (`COOKIEBOT.py:306-307`
  decrements, then tests). So the seventh consecutive trigger is the one that
  goes unanswered, and it is answered with **nothing at all** — no notice, no
  error. That silence is v1 behaviour and is preserved.
- **R4.4** Replenish: `bump_clamped(delta=+1, ...)` on any other group text
  message this router sees — v1 does it in the `else` of the same chain
  (`COOKIEBOT.py:313`), which is precisely "a text message that reached the AI
  branch and did not trigger it". R5.2's router placement is what makes that
  equivalence hold.
- **R4.5** The voice path never touches the counter (`COOKIEBOT.py:160-162` has
  no `decrease` call). Preserved.
- **R4.6** `None` from `bump_clamped` fails open — the bot answers.

## R5 — Handler and registration

- **R5.1** New module `cb_gateway/handlers/chat_ai.py`, one `Router`.
- **R5.2** **Registration point: in `build_router`'s "content rules" block,
  after `stickerspam` and immediately BEFORE `embedder`.** This is load-bearing
  and easy to get wrong. v1 runs `check_reply_embed` inside the `else` that is
  only reached when the AI branch did *not* match (`COOKIEBOT.py:309-316`), so
  the embed rewrite must sit downstream of the AI trigger, not upstream. Every
  branch that intercepts ahead of the AI in v1 (`:290` welcome prompt, `:293`
  rules prompt, `:296` `who`, `:298` captcha reply, `:300` complaint reply,
  `:302` `reply_markup` reply) is already registered earlier in v2's
  `build_router`, so the existing order reproduces v1's precedence for free —
  do not reorder anything else.
- **R5.3** Two handlers **in this order** inside the router:
  1. `ai_reply` — `F.chat.type != ChatType.PRIVATE`, `F.text`,
     `FeatureGate("fun")` (`cb_gateway/filters.py`), and the `MentionsBot`
     filter of R5.4.
  2. `replenish` — `F.chat.type != ChatType.PRIVATE`, `F.text`; does R4.4 and
     then `raise SkipHandler`, so everything downstream still runs. `members.py:56-72`
     is the precedent for a handler that yields rather than consumes.
  With `fun` off, `ai_reply` does not match, the message falls to `replenish`,
  and **no `fun_off` notice is sent** — v1 sends none on this path
  (`COOKIEBOT.py:304`, contrast `:218-219`), and v1 still replenishes in that
  case. Do **not** use `deny_if_disabled` here.
- **R5.4** `MentionsBot` filter, local to `chat_ai.py`. Matches when either:
  the message is a reply to a message from this bot that has text; or the text
  contains, case-insensitively, the skin's display name or its `@username`.
  Both come from what the dispatcher already has —
  `tenancy.registry.by_skin(skin).display_name` and the `bot_username` kwarg —
  rather than v1's hardcoded `"cookiebot"`/`"@CookieMWbot"`/`"@pawstralbot"`
  literals (D-AI-7). For the `cookiebot` skin this is byte-for-byte v1's
  trigger; for `bombot` it is the behaviour v1's hardcoding never gave it.
- **R5.5** Preparation, in order: `bot.send_chat_action(chat_id, "typing")`
  (v1 `NaturalLanguage.py:66`); strip the same tokens the filter matched on;
  newlines become spaces; `.strip()`. **No `.capitalize()`** (D-AI-3). If the
  result is empty, reply the literal `"?"` and make **no model call** — v1
  parity (`NaturalLanguage.py:74`).
- **R5.6** Message assembly for `router().complete("chat", ...)`:
  - `system=` the persona of R6.
  - When the trigger is a reply to a bot text, that text goes in as an
    **`assistant`** message. Never `system` — D-AI-5 is a live prompt-injection
    hole in v1 and this is the fix.
  - The user's stripped text, plus the brevity line verbatim from v1
    (`NaturalLanguage.py:26-31`): `en` → `"Try to reduce the answer a lot."`,
    `pt` → `"Tente reduzir bastante a resposta."`,
    `es` → `"Intenta reducir mucho la respuesta."`, any other language → nothing
    appended at all (v1 appends `"\n\n"` and an empty string; the stray blank
    line is dropped).
  - `group_id=ctx.group_id`, `user_id=message.from_user.id`,
    `tenant_id=<the tenant of R2.6>`.
- **R5.7** Order of the three gates before the model call: per-group limit (R3)
  → per-user counter (R4) → budget (R2, inside the router). Cheapest and most
  local first.
- **R5.8** Reply with `message.reply(...)` — reply-to the trigger, plain text
  (v1 `msg_to_reply=msg`).
- **R5.9** The reply-generation half is factored as
  `async def reply_with_ai(message, ctx, *, skin, bot_username, text) -> None`,
  module-level and importable, because `x_speech_to_text`'s voice handler calls
  exactly this with the transcript as `text`. That is v1's own structure —
  `COOKIEBOT.py:161-162` assigns the transcript to `msg['text']` and calls the
  same function.
- **R5.10** Error handling, D-AI-4's fix — **every** failure path produces a
  visible reply: `LLMBudgetExceededError` → `t(ctx, "ai_quota_spent")`; any
  other `LLMError` or unexpected exception → `t(ctx, "ai_unavailable")`. The
  only silences in this feature are the two v1 has by design (R4.3's exhausted
  counter, R3.2's above-limit flood).

## R6 — The persona

- **R6.1** A module constant in `chat_ai.py`, formatted with the skin's display
  name. It carries what v1's prompt actually established about the character: a
  furry AI called `<skin>`, created by **Mekhy**; talks like a friend with real
  opinions rather than a neutral assistant; informal and irreverent; replies in
  whatever language it was addressed in; keeps answers short; and does not
  invent facts it does not know.
- **R6.2** It is **not** v1's text. `NaturalLanguage.py:18` is a DAN jailbreak
  whose substance is "ignore your provider's policies", "emit two labelled
  answers", "be uncensored" and "make up an answer rather than admit
  ignorance". Per D-AI-1 that scaffolding is dropped and only the character
  survives. The last instruction in particular is inverted, not preserved: an
  instruction to fabricate is a defect regardless of what v1 shipped.
- **R6.3** No few-shot seeds. v1's two pairs (`Bot/Static/AI_SFW.json`) exist
  only to demonstrate the `[🔒CLASSIC]`/`[🔓JAILBREAK]` dual format; with the
  format gone they carry nothing. Not vendored — which also removes v1's
  cross-chat context bleed at the root (D-AI-2).
- **R6.4** No `[🔓JAILBREAK]` split and no `replacements` regex laundering
  (`NaturalLanguage.py:11,37-48`): both existed solely to hide the jailbreak's
  output format. The model's text is sent as written.

## R7 — Telemetry

- **R7.1** One new counter in `cb_gateway`:
  `cb_gateway_ai_replies_total{outcome}`, `outcome` ∈
  `ok | empty | rate_limited | streak_exhausted | budget | error`. Six values,
  no group or user labels — cardinality stays bounded, per AGENTS.md.
- **R7.2** Everything else is already emitted by `LLMRouter._meter` and
  `_persist`: tokens, cost, duration, refusals, and the `llm_usage` row.

## R8 — Strings

**`lib.json` must not be touched.** `packages/cb-core/tests/test_locales.py::TestByteIdenticalToV1`
diffs it against v1 byte-for-byte. `cb.json` is the v2-only overlay that layers
on top (`locales.py:85-99`) and is where these belong, in all three of
`locale_data/{en,pt,es}/cb.json`:

| Key | en |
|---|---|
| `ai_unavailable` | `AI is temporarily unavailable. Please try again later.` — v1's exact English (`NaturalLanguage.py:36`); `pt`/`es` are authored, since v1 never translated it |
| `ai_quota_spent` | the tenant's LLM budget for the month is spent |
| `ai_rate_limited` | too many questions in this group right now |

## R9 — Open decisions, answered

1. **Does langchain replace the hand-rolled providers?** No — R1.7/R1.8. Only
   the `chat` task moves. `moderate` stays where it is so `doomlist`, the one
   live LLM consumer, is untouched by this slice.
2. **What about transcription?** langchain has no portable STT interface, so
   `transcribe` stays on the OpenAI provider and the langchain provider raises
   for it (R1.5). Recorded as a limit, not hidden.
3. **Is the per-user counter per group or global?** Global, per v1's dict
   (R4.2).
4. **Fail open or closed on a budget-check failure?** Open, with the reasoning
   written out in R2.4.
5. **Private chats?** Out of scope — v1 returns before the chain is reached
   (`COOKIEBOT.py:110`), so there is nothing to port. `private_context.py`
   exists if someone later wants a DM shape; this feature does not use it.
