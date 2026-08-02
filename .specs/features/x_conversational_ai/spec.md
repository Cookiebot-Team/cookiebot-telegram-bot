# x_conversational_ai — Specify

**Feature id:** `x_conversational_ai` · **Milestone:** M3 · **Kind:** state report
**Status:** `partial` — the generic LLM plumbing this feature would use is
built and tested; nothing calls it for this purpose, and no acceptance
criteria exist anywhere to say what "done" looks like.

This is not a build spec. It records what exists, what doesn't, and why.

## What is actually implemented today

- `cb_core.llm.router()` — task-based routing, per-call metering (Prometheus
  + a per-group `llm_usage` row) and a circuit breaker —
  `packages/cb-core/src/cb_core/llm/router.py`. The `chat` task this feature
  would use defaults to `claude-opus-5` at low effort
  (`router.py:57`, `DEFAULT_TASKS["chat"]`).
- Unit tested at the router layer: task config drives the call, an unknown
  task errors, an unconfigured provider raises `LLMUnavailableError`, a
  refusal is returned rather than raised, and the circuit breaker opens after
  repeated failures — `packages/cb-core/tests/test_llm.py:241-278`
  (`TestLLMRouter`).

That is the entire footprint. No code outside `cb_core/llm/` references this
feature.

## What is missing

- **No handler.** Neither `cb_gateway/handlers/` nor `cb_worker/jobs/`
  contains anything that calls `router.complete("chat", ...)` — confirmed by
  listing both directories (`packages/cb-gateway/src/cb_gateway/handlers/`,
  `packages/cb-worker/src/cb_worker/jobs/`) and grepping for `llm.router`
  imports outside `cb_core`, `cb_gateway/main.py`,
  `cb_gateway/telemetry.py`, and `cb_gateway/handlers/doomlist.py` (a
  different task — `moderate` for doomlist, unrelated to this feature).
- **No acceptance coverage exists anywhere**, not just in v2. Neither
  `../Cookiebot-QA/features/` nor `qa/features/` has ever had a scenario for
  conversational AI — it's one of the 20+ v1 features
  `docs/site/content/docs/feature-map.mdx` §4 lists as implemented but never
  spec'd in QA. There is no acceptance bar written down anywhere to say what
  a port needs to satisfy.
- **No quota.** v1 has none either — there's no rate limit anywhere in
  `NaturalLanguage.py` — but v2 would need an explicit decision about whether
  the flagship model's per-call cost needs one before this is exposed
  publicly (AGENTS.md's cost-metering emphasis exists precisely because v1
  never metered this at all). Nobody has made that decision.
- **v1's trigger is two features tangled into one code path.** A text mention
  of "cookiebot" in a group message (`COOKIEBOT.py:187-188`, substring match)
  and a voice reply to the bot's own message
  (`COOKIEBOT.py:160-161` — the reply is transcribed via `speech_to_text`,
  then the transcript is fed to the same `conversational_ai` function) both
  end up here. `NaturalLanguage.py:65-77` (`conversational_ai`) only covers
  what happens once there's text; the voice half depends on
  `x_speech_to_text`'s handler existing first, which it doesn't either (see
  that feature's spec).
- **v1's NSFW branch has no v2 seam at all.** `conversational_model_nsfw`
  (`NaturalLanguage.py:55-63`) calls a third-party service
  (`api.simsimi.vn`) with no v2 equivalent — not a "handler not written" gap
  like the rest of this feature, but a real behavioural hole: there is
  nothing in `cb_core/llm` a port could route this branch to today.

## Why it stopped there

The router landed as M0/M1 shared infrastructure for *every* future LLM
consumer — moderation (`doomlist`'s `moderate` task), and eventually this —
ahead of any specific feature needing it, the same "build the plumbing once"
order `platform_tenancy` and `platform_llm` followed. The reason this
particular handler stopped, rather than just hasn't-started, is a real
dependency: HANDOFF §1.2 notes the private-chat dispatch mechanism this and
several other M3 features want only just landed, with no first consumer
built against it yet. Nobody has picked conversational AI as that consumer.
Past that, there's also no written QA scenario to build toward — someone has
to write the acceptance criteria before "done" has a shape.

## What it would take to finish, and what blocks it

Nothing here is blocked on missing infrastructure — the router, the
provider, and the private-chat pattern all exist:

1. Write the QA scenario — there's nothing to port, it has to be authored
   from v1's observed behaviour, per AGENTS.md §6.
2. Write the handler: mention-trigger detection ported from
   `COOKIEBOT.py:187-188`'s substring match, `router.complete("chat", ...)`
   for the reply.
3. Decide and implement a quota.
4. Decide what, if anything, replaces the NSFW branch's third-party
   dependency — or explicitly drop it and record that as a behavioural
   change, the way `util_everyone` recorded dropping D-EV-5.

## v1 equivalent

`../COOKIEBOT-Telegram-Group-Bot/Bot/NaturalLanguage.py:65-77`
(`conversational_ai`, dispatching to `conversational_model_sfw`/`_nsfw` at
`:17-63`), triggered from
`../COOKIEBOT-Telegram-Group-Bot/Bot/COOKIEBOT.py:187-188` (text mention) and
`:160-161` (voice reply, via `x_speech_to_text`).
