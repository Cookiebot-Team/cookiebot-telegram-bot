# util_youtube — Design

## R1 — split

**R1.1** Gateway (`packages/cb-gateway/src/cb_gateway/handlers/youtube.py`):
`ctx.enabled("utility")` gate, then the no-query check (`youtube_need`) —
both free, synchronous, matching v1's own order. A real query enqueues
`jobs.YOUTUBE_SEARCH` with `group_id`, `message_id` (for the reply-to),
`query`, `lang` — the same scalar-payload discipline `util_everyone`/
`util_calladms` established, no HTTP call on the reply path at all.

**R1.2** Worker (`packages/cb-worker/src/cb_worker/jobs/youtube.py`):
`search_youtube(ctx, *, group_id, message_id, query, lang)`, wrapped in the
same span/`job_duration`/`log.exception` shape `everyone.py`/`calladms.py`
copy (not import, same reasoning both already give: `main.py` imports each
job module to register it, so a job module must not import back).

## R2 — the API call

**R2.1** Direct `httpx` GET to `https://www.googleapis.com/youtube/v3/search`
(`part=snippet&q=<query>&type=video&maxResults=10&key=<settings.youtube_api_key>`),
timeout `settings.youtube_timeout_seconds` (new setting, default 5s — v1 had
no timeout at all, D-YT-1's fix extends to giving the call an actual bound,
not just moving it off the reply path). No `google-api-python-client`: v2
already has `httpx` for every other outbound HTTP call in this codebase
(AGENTS.md §5, "do not add a dependency that duplicates one already
present"), and the REST surface here is one GET with four query params.

**R2.2** A request failure, a timeout, an empty `youtube_api_key`, or zero
`items` in the response are all the same outward behaviour: `youtube_no_find`.
v1 has no distinct "the search itself is broken" string, and manufacturing
one is not this port's call — this mirrors the "degrade to the nearest
existing honest string" policy `calladms.py`'s `admin_usernames` and
`battle.py`'s `battle_extract` already established for a failed external
call. A request-level failure is logged (`log.warning`); a genuine zero
results is not — same distinction `admins.py` draws between an outage and a
real answer.

## R3 — telemetry

**R3.1** `cb_worker_youtube_search_total{outcome}`, outcome in
`sent|not_found|error` — mirrors `calladms_dm_total`'s shape. No
group/query label (AGENTS.md §7).

## R4 — reaction and reply shape

**R4.1** No-results reaction: v1's `react_to_message(msg, '🤷', is_big=False)`
targets the *original* message from a context that only has `message_id`
(the job, not a live aiogram `Message`) — `bot.set_message_reaction(group_id,
message_id, reaction=[ReactionTypeEmoji(emoji="🤷")], is_big=False)`, the
direct Bot API equivalent, best-effort suppressed like every other reaction
in this codebase.

**R4.2** Both outcomes reply to `message_id` (`reply_parameters` /
`reply_to_message_id` — v1's `msg_to_reply=msg` in both branches), `HTML`
parse mode for the success case.

## Open decisions — answered

1. **REST call, not the SDK.** R2.1.
2. **Failure degrades to `youtube_no_find`.** R2.2 — no new locale string.
3. **Reaction via `set_message_reaction`, not `message.react`.** R4.1 — the
   job only has a `message_id`, not a live `Message` object.
