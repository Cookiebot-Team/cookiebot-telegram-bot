# Contract: x_conversational_ai (v1 -> v2)

Phase 2/6 of `/migrate-feature` for the mention/reply-triggered AI persona.
QA: authored locally, `qa/features/x_conversational_ai.feature` — no scenario
exists in `../Cookiebot-QA/features/` for this feature, confirmed against the
full listing. FEATURE-MAP row: `x_conversational_ai`. Spec/design:
`.specs/features/x_conversational_ai/{spec,design,tasks}.md` — read those for
the full reasoning; this file is the durable behaviour record. This feature
ships in the same slice as `x_speech_to_text` (`docs/contracts/
x_speech_to_text.md`), whose voice trigger calls this feature's
`reply_with_ai` directly.

Files owned by this port: `packages/cb-core/src/cb_core/llm/
langchain_provider.py` (new), `packages/cb-core/src/cb_core/llm/budget.py`
(new), `packages/cb-core/src/cb_core/llm/{types,router}.py` (edit),
`packages/cb-core/src/cb_core/cache.py` (`bump_clamped`, edit),
`packages/cb-core/src/cb_core/locale_data/{en,pt,es}/cb.json` (edit),
`packages/cb-core/src/cb_core/settings.py`, `.env.example` (edit),
`packages/cb-gateway/src/cb_gateway/handlers/chat_ai.py` (new),
`packages/cb-gateway/src/cb_gateway/handlers/__init__.py` (one router line),
and the tests listed below.

## Phase 1 — where v1 lives

- Handler: `conversational_ai`, `../COOKIEBOT-Telegram-Group-Bot/Bot/
  NaturalLanguage.py` (whole file).
- Dispatch: `COOKIEBOT.py:304-308` (text), `:155-162` (voice, via
  `x_speech_to_text`) — inside the non-command `elif` chain, gated on
  `funfunctions`, with five earlier branches (`/newwelcome`/`/newrules`
  reply prompts, `who`, a captcha-caption reply, a complaint reply, a
  `reply_markup` reply) that intercept ahead of it.
- Quota: `Cooldowns.py:5,8,24-36` — a per-user, process-global signed
  counter, `MAX_CONSECUTIVE_RESPONSES_AI = 7`.

## Phase 2 — v1 behaviour contract

| Aspect | v1 behaviour (file:line) |
|---|---|
| Triggers (text) | Inside the non-command branch: `funfunctions and ((reply to a bot message that has text) or "cookiebot" in text.lower() or "@CookieMWbot" in text)` — `COOKIEBOT.py:304`. Substring, anywhere in the message; `"cookiebot"` case-insensitive, `"@CookieMWbot"` case-**sensitive**. |
| Triggers (voice) | `content_type == "voice"` **and** the voice message is a reply to one of the bot's own messages **and** `funfunctions` — `COOKIEBOT.py:155-162`. The audio is transcribed (`x_speech_to_text`), the transcript is assigned to `msg['text']`, and that text is fed to the same `conversational_ai`. |
| Preconditions | `funfunctions` only. **No** `fun_off` notice when it is off — the branch is simply skipped, unlike the `/`-command paths at `COOKIEBOT.py:218-219`. Group/supergroup only. No admin check. |
| Dispatch order | Last but one in the non-command chain — five branches intercept first (`:290`, `:293`, `:296`, `:298`, `:300`, `:302`) and, when they match, the AI never runs even though `"cookiebot"` may be in the text. |
| Quota | A signed per-user counter, not a time cooldown. `MAX_CONSECUTIVE_RESPONSES_AI = 7`, range `[-7, 7]`, process-global (`Cooldowns.py:5,8`). Every text trigger decrements; every ordinary non-triggering message increments. The gate is `remaining > 0` — at or below zero the bot says **nothing at all**. The voice path never decrements. |
| Message preparation | `send_chat_action(..., 'typing')` first. Trigger words stripped (`"Cookiebot"`, `"cookiebot"`, `"@CookieMWbot"`, `"COOKIEBOT"`, `"CookieBot"`, plus `"@pawstralbot"` — asymmetric sets). Newlines become spaces, then `.strip().capitalize()`. Empty result ⇒ literal `"?"`, no API call. |
| Model call | OpenAI `gpt-4o-mini`, `temperature=1`, `timeout=10`, no `max_tokens`. |
| System prompt | A DAN jailbreak, in Portuguese, always — see **D-AI-1**. Plus `f"Hoje é {now:%A}."`. |
| Few-shot | Two seed pairs from `Bot/Static/AI_SFW.json`, rotated on every call from every chat — see **D-AI-2**. |
| Conversation context | A replied-to bot text is injected as an extra **`system`**-role message — see **D-AI-5**. |
| Length instruction | Appended verbatim per language: `eng`/`pt`/`es` strings, any other language → empty (trailing `"\n\n"`). |
| Post-processing | Split on `"[🔓JAILBREAK]"`; regex laundering (`dan`→`cookie`, `chatgpt`→`cookiebot`); strip a matched quote pair and a trailing `"."`; `.capitalize()` — see **D-AI-3**. |
| Success output | Sent as a reply to the triggering message, plain text. |
| Failure output | Hardcoded English string, only for three OpenAI exception types — see **D-AI-4**. Any other exception (timeouts included) escapes with no reply. |
| Persistence | None. |
| External calls | OpenAI chat completions. `api.simsimi.vn` in `conversational_model_nsfw` — dead code, unreachable, see **D-AI-6**. |

## Defects — verdict per item

| id | Defect | Verdict |
|---|---|---|
| D-AI-1 | System prompt is a DAN jailbreak: ignore provider policy, emit two labelled answers, be uncensored, and explicitly invent facts rather than admit ignorance (`NaturalLanguage.py:18`). | **fix — not ported.** v2 keeps the persona the jailbreak carried (a furry AI called after the skin, created by Mekhy, an opinionated friend rather than a neutral assistant, informal, replies in whatever language it was addressed in, short answers) and drops the jailbreak scaffolding, the dual-response format, and inverts the fabricate-when-unsure instruction. |
| D-AI-2 | `questions_list`/`answers_list` are process-global, mutated on every call with no lock, so one chat's conversation leaks into another's few-shot context. | **fixed by omission.** v2 holds no cross-request conversational state at all: no few-shot seeds, ever. |
| D-AI-3 | `.capitalize()` on the finished answer lowercases everything after the first character. | **fix.** The model's text is sent exactly as written. |
| D-AI-4 | Only three OpenAI exception types are caught; anything else (timeouts included) leaves the user with no reply. | **fix.** Every failure path replies — `LLMBudgetExceededError` gets `ai_quota_spent`, everything else gets `ai_unavailable`. |
| D-AI-5 | A replied-to bot text is injected as a **`system`**-role message, so any user can plant arbitrary system instructions by replying to a bot message. | **fix.** Prior turns go in as `assistant`; the user's text as `user`. No user-controlled content is ever given system authority. |
| D-AI-6 | `conversational_model_nsfw` calls `api.simsimi.vn`; unreachable — `grep` finds no call site. | **dropped, recorded.** No v2 equivalent, no third-party dependency added. Dropping unreachable code changes nothing a user could observe. |
| D-AI-7 | `.capitalize()` and the reply-chain rules make the trigger-word sets asymmetric; both the dispatch check and the stripper hardcode `"cookiebot"`/`"@CookieMWbot"`/`"@pawstralbot"` literals. | **fix, generalised.** v2 derives both the trigger and the stripped tokens from the skin the message arrived on (`tenancy.registry.by_skin`): its display name and its `@username`, matched case-insensitively. Byte-identical to v1 for the `cookiebot` skin; a real trigger for `bombot`, which v1's hardcoding could never give it. |
| D-AI-8 | No rate limit beyond the per-user counter, no cost metering of any kind. | **fix, additive.** v1's per-user counter is ported as observable behaviour. On top, v2 adds a per-group rate limit and enforces the tenant spend cap. |

## The langchain provider — a new shape underneath `chat`, nothing else moves

`LangchainProvider` (`packages/cb-core/src/cb_core/llm/langchain_provider.py`)
implements the `LLMProvider` protocol in full, resolving fully-qualified
`"provider:model"` strings (`"anthropic:claude-opus-5"`) through
`langchain.chat_models.init_chat_model`, with resolved clients cached per
`(model, max_tokens, temperature, timeout)`. It sits behind `LLMRouter`
exactly like the two hand-rolled providers: same protocol, same
per-provider breaker, same metering, same `_persist` usage row — nothing
downstream of `router().complete()` can tell which provider served a call.

**Only `DEFAULT_TASKS["chat"]` moves** to
`TaskConfig(provider="langchain", model="anthropic:claude-opus-5",
max_tokens=1024, temperature=1.0, timeout=30.0)` — `temperature=1.0` is v1's
own value, and it is **filtered out before it reaches the wire**: the
catalogue records that current Claude models return 400 for sampling
parameters, so `_resolve` gates `temperature` on
`catalog.spec_for(...).supports_sampling` exactly as `anthropic_provider.py`
does. The task still states the intended value, so repointing `chat` at a
model that does accept it stays a config change rather than a code one.
`moderate`, `summarize`, `vision` and `transcribe` keep their
existing hand-rolled providers untouched, so `util_doomlist`'s live
`moderate` calls are not touched by this slice. `effort` is dropped from the
`chat` task: there is no portable effort parameter across langchain's
vendor integrations, and carrying one that only works for a single backend
would defeat the point of the abstraction.

**Transcription is a deliberate limit, not an oversight.** langchain has no
portable speech-to-text interface across its chat-model integrations, so
`LangchainProvider.transcribe()` raises
`LLMError("langchain provider does not offer speech-to-text; route to
openai")`, mirroring `anthropic_provider.py`'s own shape for the same
method. The `transcribe` task stays on the hand-rolled OpenAI provider —
see `docs/contracts/x_speech_to_text.md`.

## The tenant budget cap — the first thing to ever read `monthly_llm_budget_usd`

`Tenant.monthly_llm_budget_usd` has had a column, a struct field and a
docstring since migration `0003_tenants.py:40` and was never read by
anything. `packages/cb-core/src/cb_core/llm/budget.py` is the first reader,
and it is now enforced, hard, in both `LLMRouter.complete()` and
`LLMRouter.transcribe()`: over budget refuses with
`LLMBudgetExceededError`, told to the user (`ai_quota_spent`/
`transcribe`'s own failure path); a tenant with no budget configured is
never checked at all (`tenant_id=None` skips the check entirely, so no
existing caller changes behaviour).

**Failure direction is not symmetric, on purpose.** Over budget per a spend
query that *succeeded* ⇒ refuse — that is the cap doing its job. A cache or
database *failure* while computing the spend ⇒ **allow**, log
`llm.budget_check_failed`, count it (`cb_llm_budget_check_failed_total`). An
infrastructure outage is not evidence of overspend — the same fail-open
reasoning `stickerspam._bump` already established for `incr_window`
returning `None`. The spend aggregate is month-to-date UTC (the nightly
`llm_daily_cost` rollup plus today's `llm_usage` rows the rollup hasn't
folded in yet), cached in Valkey under `cb:llm:mtd:{tenant_id}`.

**That aggregate does not sit on the reply path.** It filters on
`tenant_id` with no `group_id` predicate, so Citus fans it out across every
shard — precisely what AGENTS.md §4 rule 1 exists to keep off a hot query.
A cached value older than 60s is served immediately and refreshed in the
background, deduped per tenant. Only a genuinely empty cache blocks, once,
and it does **not** fail open there: "not yet computed" is not an
infrastructure failure, and allowing a brand-new tenant's first burst to
all read zero spent would be a hole straight through a cap that is
supposed to be hard. The long-term fix is a worker rollup into
`tenant_monthly_cost`, which has existed unpopulated since
`0003_tenants.py:78-92`; that job is not built here.

## The per-user consecutive counter — v1 parity on a new primitive

v1's real quota (`Cooldowns.py:5,8,24-36`) is ported as observable
behaviour: a signed counter in `[-7, 7]`, keyed **per user, not per
group** (v1's `remaining_responses_ai` is a process-global dict keyed only
by `msg['from']['id']`). Neither `cb_core.cooldowns`'s in-process objects
(no shared store, the gateway runs replicated) nor `cache.incr_window`
(increment-only) can serve it, so `cb_core/cache.py` gains one new
primitive: `bump_clamped(key, delta, *, lo, hi, initial, ttl_seconds)`, an
atomic Lua `EVAL` that seeds a missing key to `initial`, applies `delta`,
clamps into `[lo, hi]`, refreshes the TTL, and returns the new value —
`None` on any Valkey error, the same fail-open contract `incr_window`
already has.

Key `cb:ai:streak:{user_id}`, TTL 86400s. Spend is `delta=-1`; the gate is
the **post-decrement** value `> 0`, exactly v1's order — the seventh
consecutive trigger is the one that goes unanswered, with **no notice at
all**, v1's own silence, preserved. Replenish is `delta=+1` on any other
group text message this router sees (`replenish`, R4.4) — v1's `else` of
the same chain. The voice path never touches the counter, matching v1's
absent `decrease` call on that branch. `None` from `bump_clamped` fails
open — the bot answers.

## Handler shape and registration

`packages/cb-gateway/src/cb_gateway/handlers/chat_ai.py` carries two
handlers in one router, in order: `ai_reply` (the mention/reply trigger,
gated on `FeatureGate("fun")` and `MentionsBotFilter`) then `replenish`
(bumps the streak `+1` for every other group text message, then `raise
SkipHandler`). Gate order ahead of the model call: per-group window → the
per-user streak → the tenant budget (checked inside the router call).
`reply_with_ai(message, ctx, *, skin, bot_username, text)` is the
module-level, importable half that actually calls the model and replies —
`x_speech_to_text`'s voice handler calls it directly with the transcript as
`text`, v1's own structure (`COOKIEBOT.py:161-162` assigns the transcript
to `msg['text']` and calls the same function text triggers use).
`reply_with_ai` never touches the per-group window or the per-user streak
— those gate the *trigger decision*, which is `ai_reply`'s job, not the
reply's; this is what makes the voice path's exemption from the streak
(v1 parity) automatic rather than a flag callers must remember to pass.

**Registration is load-bearing**: `chat_ai.router` sits in
`build_router`'s "content rules" block, after `stickerspam` and
immediately before `embedder` — v1 only runs `check_reply_embed` in the
`else` reached when the AI branch did *not* match, so the embed rewrite
must sit downstream of the AI trigger. Every branch that intercepts ahead
of the AI in v1 is already registered earlier in v2's `build_router`.

## Phase 6 — parity table

| Aspect | Verdict |
|---|---|
| Text trigger substring match, case sensitivity by token | **same in substance, generalised to any skin** — D-AI-7 |
| Voice trigger (reply-to-bot + `funfunctions`) | **same** — the sub-step this feature receives from `x_speech_to_text` |
| `funfunctions` gate, no `fun_off` notice when off | **same** |
| Dispatch order relative to earlier-intercepting branches | **same** — v2's existing `build_router` order already reproduces it |
| Per-user consecutive counter (`[-7, 7]`, silent exhaustion, voice exempt) | **same, ported as observable behaviour** — on a new `cache.bump_clamped` primitive since v1's process-global dict cannot survive a replicated gateway |
| Trigger stripping, newline handling, empty-message `"?"` | **same**, minus `.capitalize()` (D-AI-3, fix) |
| Model call (provider, temperature) | **changed (intentional)** — routes through `cb_core.llm.router()`'s new langchain-backed provider instead of calling OpenAI directly; `temperature=1.0` preserved from v1 |
| System prompt | **changed (intentional, fix)** — D-AI-1: the DAN jailbreak is not ported; only the character persona survives, and the fabricate-when-unsure instruction is inverted |
| Few-shot seeds | **dropped (intentional, fix)** — D-AI-2: no cross-request conversational state of any kind |
| Conversation context role (reply-to-bot text) | **changed (intentional, fix)** — D-AI-5: `assistant`, never `system` |
| Length instruction strings | **same, verbatim** per language; unknown-language trailing `"\n\n"` dropped |
| Post-processing (jailbreak split, regex laundering, `.capitalize()`) | **dropped (fix)** — D-AI-3/R6.4: the model's text is sent as written |
| Failure handling | **changed (intentional, fix)** — D-AI-4: every failure path replies, not just three OpenAI exception types |
| NSFW/simsimi branch | **dropped, recorded** — D-AI-6: unreachable in v1, no v2 equivalent |
| Cost control | **changed (additive)** — D-AI-8: v1's counter, plus a new per-group rate limit and the tenant hard spend cap (`Tenant.monthly_llm_budget_usd`, enforced for the first time) |
| Reply shape (reply-to-trigger, plain text) | **same** |

## QA

No v1 or `Cookiebot-QA` scenario exists for this feature (confirmed against
the full `../Cookiebot-QA/features/` listing). `qa/features/
x_conversational_ai.feature` is authored directly against `spec.md`'s "QA —
authored, not ported" section and `design.md`'s R3/R4/R5 gates. Seven
scenarios: a mention triggers a reply; a reply-to-bot message triggers a
reply; `fun` off is silent, not a `fun_off` notice; an empty stripped
message answers `"?"` with no model call; an earlier-intercepting branch
(a `/newrules` reply prompt from a non-admin) wins over the AI branch; the
per-user counter silences the bot after seven consecutive triggers; an
ordinary message replenishes an exhausted counter.

## Tests

| Layer | File |
|---|---|
| Unit — langchain provider (`complete`/`stream`/`count_tokens`, `transcribe` raising, cost lookup with the provider prefix stripped) | `packages/cb-core/tests/test_llm_langchain.py` |
| Unit — tenant budget (`month_to_date_usd`, `ensure_within_budget`, fail-open on cache/DB error) | `packages/cb-core/tests/test_llm_budget.py` |
| Unit — `cache.bump_clamped` (seed, clamp at both ends, TTL refresh, fail-open) | `packages/cb-core/tests/test_cache.py` |
| Unit — locales/settings | `packages/cb-core/tests/test_locales.py`, `test_settings.py` |
| Unit — router (`complete`/`transcribe` budget wiring) | `packages/cb-core/tests/test_llm.py` |
| Unit — `MentionsBot`, trigger stripping, brevity line, message assembly (role of a reply), handler gates | `packages/cb-gateway/tests/test_chat_ai.py` |
| Acceptance — the seven scenarios above, against the real dispatcher and a stubbed router; the per-user streak and per-group window run against real Valkey (db 15) | `qa/features/x_conversational_ai.feature`, `qa/test_x_conversational_ai.py` |

**A real Postgres is needed by every scenario in this file**, for a reason
unrelated to this feature: `core_groupguardian`'s captcha-reply filter runs
ahead of `chat_ai` in `build_router` and does its own `get_config` +
pending-captcha-row lookup over *every* plain group text message whenever
`captcha_timeout_seconds > 0` — the v1-matching default, on for every
scenario here. With no live pool that lookup raises instead of returning
"no pending row", crashing every scenario rather than skipping it; the
`_database` fixture exists purely to give that filter something to query
against, and skips the whole file cleanly when no database is reachable.
