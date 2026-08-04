# x_conversational_ai — Specify

**Feature id:** `x_conversational_ai` · **Milestone:** M3 · **Kind:** port
(with four recorded behavioural changes)
**v1 source:** `Bot/NaturalLanguage.py` (whole file), dispatched
`Bot/COOKIEBOT.py:304-308` (text) and `:160-162` (voice, via
`x_speech_to_text`).

This supersedes the state-report that lived here. The state report's findings
are folded into "What already exists" below; everything else is new.

## Goal

A group member mentions the bot, or replies to something the bot said, and the
bot answers in character. v1 did this with OpenAI directly; v2 routes it
through `cb_core.llm`, so it is metered, traced, breaker-protected and
provider-agnostic like every other LLM call in the tree.

## What already exists (do not rebuild)

- `cb_core.llm.router()` — task routing, per-call metering (Prometheus + a
  per-group `llm_usage` row), circuit breaker, refusal fallback.
  `packages/cb-core/src/cb_core/llm/router.py`.
- Nothing outside `cb_core/llm/` calls the `chat` task. No handler, no job.
- No QA scenario exists for this feature in **either** repo — confirmed against
  the full `../Cookiebot-QA/features/` listing. The scenarios in
  `qa/features/x_conversational_ai.feature` are authored here, per AGENTS.md §6,
  from v1's observed behaviour.

## Phase 2 — v1 behaviour contract

| Aspect | v1 behaviour (file:line) |
|---|---|
| Triggers (text) | Inside the non-command branch: `funfunctions and ((reply to a bot message that has text) or "cookiebot" in text.lower() or "@CookieMWbot" in text)` — `COOKIEBOT.py:304`. Substring, anywhere in the message; `"cookiebot"` case-insensitive, `"@CookieMWbot"` case-**sensitive**. |
| Triggers (voice) | `content_type == "voice"` **and** the voice message is a reply to one of the bot's own messages **and** `funfunctions` — `COOKIEBOT.py:155-162`. The audio is transcribed (`x_speech_to_text`), the transcript is assigned to `msg['text']`, and that text is fed to the same `conversational_ai`. |
| Preconditions | `funfunctions` (`functionsFun`) only. **No** `fun_off` notice is sent when it is off — the branch is simply skipped, unlike the `/`-command paths at `COOKIEBOT.py:218-219`. Group/supergroup only: private chats `return` at `COOKIEBOT.py:110`, before the chain is reached. No admin check. |
| Dispatch order | The AI branch is **last but one** in the non-command chain. Five branches intercept first and, when they match, the AI never runs even though `"cookiebot"` may be in the text: the `/newwelcome` reply prompt (`:290`), the `/newrules` reply prompt (`:293`), the `who` question form (`:296`), a captcha-caption reply (`:298`), a "Milton do RH." reply (`:300`) and a reply to a bot message carrying `reply_markup` (`:302`). |
| Quota | A signed per-user counter, not a time cooldown. `MAX_CONSECUTIVE_RESPONSES_AI = 7` (`Cooldowns.py:5`), range `[-7, 7]` in the process-global dict `remaining_responses_ai` (`Cooldowns.py:8`). Every text trigger calls `decrease_remaining_responses_ai` (`COOKIEBOT.py:306`, `Cooldowns.py:31-36`); every ordinary non-triggering message calls `increase_remaining_responses_ai` (`COOKIEBOT.py:313`, `Cooldowns.py:24-29`). The gate is `remaining > 0` (`COOKIEBOT.py:307`) — at or below zero the bot says **nothing at all**, no error, no notice. **The voice path never decrements**: STT-triggered replies are exempt. |
| Message preparation | `send_chat_action(chat_id, 'typing')` first (`NaturalLanguage.py:66`). Then the trigger words are stripped out of the text: `"Cookiebot"`, `"cookiebot"`, `"@CookieMWbot"`, `"COOKIEBOT"`, `"CookieBot"` are what the `if` checks (`:69`) but the `replace` chain also strips `"@pawstralbot"` (`:70`) — asymmetric sets. Newlines become spaces, then `.strip().capitalize()`. If the result is empty, the reply is the literal `"?"` and **no API call is made** (`:74`). |
| Model call | OpenAI `gpt-4o-mini`, `temperature=1`, `timeout=10`, no `max_tokens` (`NaturalLanguage.py:34`). |
| System prompt | A DAN jailbreak, in Portuguese, always, regardless of the group's language (`NaturalLanguage.py:18`) — see **D-AI-1**. Plus `f"Hoje é {now:%A}."` (`:19`). |
| Few-shot | Two seed pairs from `Bot/Static/AI_SFW.json`, held in the module-global `questions_list`/`answers_list` (`:9-10`), and **rotated on every call from every chat** — `pop(0)` + `append` (`:49-52`) — see **D-AI-2**. |
| Conversation context | If the trigger is a reply to a bot text that is not already a stored answer, that text is injected as an extra **`system`**-role message (`:24-25`) — see **D-AI-5**. |
| Length instruction | Appended to the user message (`:26-31`), verbatim: `eng` → `"Try to reduce the answer a lot."`, `pt` → `"Tente reduzir bastante a resposta."`, `es` → `"Intenta reducir mucho la respuesta."`, any other language → the empty string (leaving a trailing `"\n\n"`). |
| Post-processing | Split on `"[🔓JAILBREAK]"` and keep what follows if present; word-boundary regex laundering via `replacements` (`:11`) — `dan`→`cookie`, `chatgpt`→`cookiebot` in four case variants each, and `[🔒CLASSIC]`→`""`; strip whitespace; strip one matched leading/trailing quote pair; strip one trailing `"."`; `.capitalize()` the whole string (`:37-48`) — see **D-AI-3**. |
| Success output | The processed string, sent as a **reply to the triggering message** (`COOKIEBOT.py:308`/`:162`, `msg_to_reply=msg`), plain text. |
| Failure output | `"AI is temporarily unavailable. Please try again later."` — hardcoded English, returned for `RateLimitError`/`APIConnectionError`/`APIStatusError` only (`NaturalLanguage.py:35-36`). Any other exception (including `APITimeoutError`) escapes to the top-level handler at `COOKIEBOT.py:329-330`, which DMs the owner a traceback and sends the chat **nothing** — see **D-AI-4**. |
| Persistence | None. |
| External calls | OpenAI chat completions. (`api.simsimi.vn` in `conversational_model_nsfw`, `:55-63` — **dead code**, see D-AI-6.) |

## Known defects — preserve / fix verdict

| # | Defect (v1 file:line) | Verdict |
|---|---|---|
| **D-AI-1** | The system prompt is a DAN jailbreak (`NaturalLanguage.py:18`): it instructs the model to ignore its provider's policies, to emit two labelled answers (`[🔒CLASSIC]` / `[🔓JAILBREAK]`), to be uncensored, and — explicitly — to **invent facts it does not know** rather than admit ignorance. | **Fix — do not port the jailbreak.** v2 keeps the persona the jailbreak was carrying (a furry AI called **CookieBot**, creator **Mekhy**, talks like a friend with real opinions rather than a neutral assistant, informal and irreverent, replies in whatever language it was addressed in, keeps answers short) and drops the DAN scaffolding, the dual-response format and the fabricate-when-unsure instruction. Reproducing a jailbreak prompt is not a compatibility obligation, and the observable persona survives without it. Recorded as a behavioural change in the contract and in `feature-map.mdx`. |
| **D-AI-2** | `questions_list`/`answers_list` are process-global, shared by every chat and every user, and mutated (`pop(0)`/`append`) on every call with no lock under a 50-worker pool (`NaturalLanguage.py:9-10,49-52`, `COOKIEBOT.py:47`). One group's conversation leaks into another group's few-shot context. | **Fix.** Same class as **D4** in `scripts/spec.py`'s defects table. v2 holds no cross-request conversational state: the seed pairs are a frozen constant, and per-conversation context comes only from the reply chain (D-AI-5). |
| **D-AI-3** | `.capitalize()` on the finished answer (`:48`) lowercases every character after the first, wrecking acronyms, proper nouns and mixed case. | **Fix.** Send the model's text as written. |
| **D-AI-4** | Only three OpenAI exception types are caught (`:35-36`); anything else — timeouts included — leaves the user with no reply at all. | **Fix.** Every failure path produces a user-visible reply; the router's breaker and refusal fallback already exist for this. |
| **D-AI-5** | The replied-to bot text is injected as a **`system`**-role message (`:24-25`), so any user can plant arbitrary system instructions by replying to a bot message. | **Fix.** Prior turns go in as `assistant`, the user's text as `user`. No user-controlled content is ever given system authority. |
| **D-AI-6** | `conversational_model_nsfw` (`:55-63`) calls `api.simsimi.vn`; the `sfw` config flag is threaded through `conversational_ai`'s signature and never read (`:65-76`). The branch is unreachable — `grep` finds no call site. | **Drop, recorded.** No v2 equivalent, no third-party dependency added. Because the branch never executed in v1, dropping it changes nothing a user could observe; it is recorded the way `util_everyone` recorded dropping D-EV-5. The `sfw` flag keeps meaning whatever it means to other features. |
| **D-AI-7** | `.capitalize()` and the reply-chain rules make the trigger-word sets asymmetric: dispatch checks `"cookiebot"`/`"@CookieMWbot"` (`COOKIEBOT.py:304`) while the stripper handles five case variants plus `"@pawstralbot"` (`NaturalLanguage.py:69-70`). | **Fix, generalised.** v2 derives both the trigger and the stripped tokens from the **skin the message arrived on** (`BotRegistry`/`tenancy`): its display name and its `@username`, matched case-insensitively. For the `cookiebot` skin that is exactly v1's behaviour; for `bombot` it is the behaviour v1's hardcoding could never give it. |
| **D-AI-8** | No rate limit beyond the per-user counter, no cost metering of any kind (`NaturalLanguage.py` has neither). | **Fix, additive.** v1's per-user consecutive counter is ported as observable behaviour. On top of it v2 adds a per-group rate limit and enforces the tenant spend cap (HANDOFF §6.6: over budget ⇒ refuse and say so). |

## Decisions — answered

Asked and answered by the owner, 2026-08-03:

1. **NSFW branch** — dropped, recorded as a behavioural change (D-AI-6). No
   simsimi, no replacement third-party service.
2. **Cost control** — per-group rate limit **and** the hard tenant cap. Over
   budget the call is refused and the user is told the quota is spent.
3. **Provider layer** — a **langchain-backed provider behind the existing
   router**: `init_chat_model`-style multi-provider resolution and model
   routing, with `cb_core.llm`'s metering, breaker, refusal fallback and
   telemetry unchanged and every existing caller untouched. The two
   hand-rolled providers stay. Details in `design.md`.
4. **The DAN prompt** — not ported (D-AI-1). Persona preserved, jailbreak
   dropped.

## Scope

In: the text trigger, the voice trigger's second half (the reply, once
`x_speech_to_text` has produced a transcript), the persona, the per-user
counter, the per-group limit, the tenant cap, the langchain provider, unit
tests and an authored acceptance suite.

Out: `who` (`COOKIEBOT.py:296`) — a different feature that merely happens to
intercept first; `identify_music` (`core_musicdetection`); private-chat AI (v1
has none — private chats return before the chain).

## QA — authored, not ported

No v1 scenario exists. `qa/features/x_conversational_ai.feature` is written
against the contract above: mention triggers a reply, reply-to-bot triggers a
reply, `funfunctions` off is silent (not a `fun_off` notice), an empty stripped
message answers `"?"` without calling the model, an intercepting branch wins
over the AI branch, the per-user counter silences the bot after seven
consecutive triggers and an ordinary message replenishes it.
