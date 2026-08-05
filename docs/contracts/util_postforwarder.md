# Contract: util_postforwarder (v1 -> v2)

Phase 2/6 of `/migrate-feature` for the publisher's outbound half. QA:
`../Cookiebot-QA/features/util_postforwarder.feature` (plus the prose in
`features/publicador(PTBR).md`). FEATURE-MAP rows: `util_postforwarder`,
`publicador(PTBR).md`. Spec/design:
`.specs/features/util_postforwarder/{spec,design,tasks}.md`.

Shipped in one slice with `util_postgetter` and `util_deletereposts`; this
feature owns the pieces all three share — the `scheduled_posts` table, the
pending-post cache, the delivery cron and the settings.

Files owned by this port: `packages/cb-api/migrations/versions/0005_scheduled_posts.py`,
`packages/cb-core/src/cb_core/{scheduled_posts,pending_posts,publisher}.py`,
`cb_core/jobs.py` (`PUBLISHER_APPROVE`), `cb_core/settings.py` (six settings),
`cb_core/llm/router.py` (the `translate` task), the twelve `cb.json` strings,
`packages/cb-gateway/src/cb_gateway/handlers/publisher.py`,
`packages/cb-worker/src/cb_worker/jobs/publisher.py`, the registrations in
`handlers/__init__.py` and `cb_worker/main.py`, and the tests below.

## Phase 1 — where v1 lives

- `ask_publisher_command` `:57-75`, `ask_approval` `:77-92`, `prepare_post`
  `:182-221`, `deny_post` `:223-228`, `schedule_post` `:230-286`,
  `schedule_autopost` `:288-314`, `scheduler_pull` `:329-357`,
  `check_notify_post_reply` `:359-369` — all
  `../COOKIEBOT-Telegram-Group-Bot/Bot/Publisher.py`.
- Job store: `create_job`/`list_jobs`/`delete_job`/`edit_job_data` `:94-127`,
  over `Publisher.db` (SQLite, `:15-17`).
- Dispatch: `COOKIEBOT.py:205` (`/divulgar`), `:208` (`/repost`), `:303` (the
  reply relay), `:370-375` (the three callback branches), `:448-455`
  (`scheduler_check`, a 300 s `threading.Timer` chain).
- Strings: **none are in `Bot/Static/locales/`** — every one is an inline
  ternary, so they are new keys in v2's `cb.json` overlay rather than
  `lib.json`, which stays a byte-identical copy of v1's.

## Phase 2 — v1 behaviour contract

| Aspect | v1 behaviour (file:line) |
|---|---|
| Triggers | `/divulgar` `/publish` `/publicar` (`COOKIEBOT.py:205`); `/repost` `/repostar` `/reenviar` (`:207-208`); callbacks `SendToApprovalPub`, `yPub`, `nPub` (`:370-375`); a text reply to a bot message carrying a `reply_markup` (`:302-303`) |
| Preconditions | `/divulgar`: **none** — no admin check, no `functionsFun`/`functionsUtility`. `/repost`: admin, `ownerID`, or `sender_chat` present (`:290`). Callbacks: none at all (D-PF-2). |
| Cooldowns / quotas | None. `max_posts` caps live campaigns per target group at schedule time (`:264`), default 9999. |
| `/divulgar` failures | no reply ⇒ `publish_need_reply`; replied message not from a channel ⇒ `publish_not_channel`; no caption ⇒ `publish_needs_media` (`:59-70`) |
| `/divulgar` success | caches the post, forwards the requester's message into `APPROVAL_CHAT_ID`, sends `'Approve post?'` (English only, never localised) with five buttons, replies `publish_sent_for_approval` (`:71-92`) |
| Approve | renders the ad into `POSTMAIL_CHAT_ID` twice — pt caption then en, same media, same keyboard — then writes one job per consenting group at a random `hour:minute`, **tomorrow** (`:96,268-272`) |
| Render keyboard | origin channel → caption URLs → caption-entity URLs (while the whole keyboard is under 5 rows) → the author, unless `'Mekhy' in first_name` → `Mural 📬` (`:184-199`) |
| Caption pipeline | `emojis_to_numbers` → translate ×2 → `convert_prices_in_text` (BRL/USD) → `<`→`⩽`, `>`→`⩾`, `&`→`＆` → truncate 1020 → revert to the untranslated text if it contains `'Error 500 (Server Error)'` (`:192,200-209`) |
| Fan-out skips | no config row; `not publisherpost`; NSFW into an SFW group; `publisher_members_only` and the author is not in that group's register; any per-group exception (`:246-276`) |
| Report | DMs `ownerID` and the requester the schedule, then replies `publish_queued` in the requester's chat; a DM failure swaps the reply for `publish_queued_no_dm` (`:277-286`) |
| `/repost` | admin gate ⇒ `not_group_admin`; no reply ⇒ `repost_need_reply`; non-numeric arg ⇒ `repost_bad_days`; days = the arg or 9999; random hour **10-17**; react 👍; reply `repost_scheduled_days`/`repost_scheduled_nolimit`, `parse_mode='HTML'` (`:288-314`) |
| Scheduler | every 300 s: skip if not due; spend a day (delete at ≤1); delete the row if the target's `publisher_post` is off; forward from the Mural, into `thread_posts` when the chat `is_forum`; kick ⇒ delete; **any other exception ⇒ delete** (`:329-357`) |
| Reply relay | first job whose name starts with inline-keyboard row 0 col 0; DMs the poster `f"{who} replied:\n'{text}'\n\nIn chat {title}"`; replies `notify_post_reply_sent` (`:359-369`) |
| Persistence | `Publisher.db`, one unkeyed SQLite table; `cache_posts`, a module-global dict keyed by `forward_from_message_id` |
| Side effects | messages into the approval chat and the Mural, DMs to the owner and the requester, forwards into every consenting group |
| External calls | Google Cloud Translate v2 (uncaught — returns an error *page* rather than raising); exchangerate-api v6, `timeout=10`, caught per paragraph; the Java backend's `registers`/`configs` |

Verbatim strings for all three languages: `.specs/features/util_postforwarder/spec.md`.

## Defects — verdict per item

| id | Defect | Verdict |
|---|---|---|
| D-PF-1 (=FEATURE-MAP D5) | One shared SQLite connection, `check_same_thread=False`, no lock, across every worker thread, on one host | **fix** — a distributed Postgres table |
| D-PF-2 | **Anyone can press `yPub`.** The callback branch checks nothing; v1 relies on the buttons only appearing in a private chat, but a payload is a plain string that can be replayed from anywhere | **fix** — accepted only from `settings.approval_chat_id` |
| D-PF-3 | `cache_posts` is a process-global dict: lost on restart, invisible to other replicas | **fix** — Valkey, TTL'd |
| D-PF-4 | A `document` ad is cached under the key `animation` and re-sent with `sendAnimation` | **preserve** — user-visible, and it works: these ads are GIFs |
| D-PF-5 | `parse_mode='HTML'` on `sendPhoto` only; video and animation captions go unparsed | **preserve** — changing it re-renders every existing video ad |
| D-PF-6 | `convert_prices_in_text` returns the whole original text when a paragraph's currency already equals the target, discarding conversions already appended | **preserve** — pure output quirk; fixing it rewrites every mixed-currency caption |
| D-PF-7 | The `max_posts` trim deletes from the list it is counting, against an already-decremented count | **fix** — one deterministic statement, evicting oldest-first |
| D-PF-8 | Any non-kick exception during a scheduled forward deletes the row, so one transient 5xx ends the campaign | **fix** — only a kick, a missing chat, or a permanent rejection deletes |
| D-PF-9 | A day is spent before the send is attempted, so a failed forward still burns one | **preserve** — the alternative is retrying a broken target forever, and D-PF-8 covers the transient case |
| D-PF-10 | `'Mekhy' not in first_name` — a hardcoded personal name suppresses the author button | **fix** — `settings.publisher_hidden_author_names`, defaulting to `("Mekhy",)`, substring semantics kept |
| D-PF-11 | `scheduler_pull` runs from a recursive `threading.Timer` in the primary process only; a crash between ticks stops every scheduled post forever, silently | **fix** — arq cron |
| D-PF-12 | The members-only gate runs `username not in str(members)` — a substring test over the stringified list, so `bob` passes in any group containing a `bobby` | **fix** — set membership |

## What moved, and what that changes

**The reply path keeps only what the group sees at once**: `/divulgar`'s three
precondition branches, the forward into the approval chat and its prompt,
`/repost`, and the reply relay. `publisher_approve` (arq) does the render and
the fan-out; a five-minute cron does delivery. v1 did all of it inline in a
callback handler and a `threading.Timer` — AGENTS.md §2.4 in every clause at
once.

**Translation goes through `cb_core.llm.router()`'s new `translate` task**, not
Google Cloud Translate. v2 already owns a provider abstraction with per-tenant
budget enforcement, retries and a breaker; `google-cloud-translate` would be a
second way to reach a third party for one call (AGENTS.md §5). The mechanism
differs, the contract does not: a pt caption and an en caption, falling back to
the untranslated text on failure — which v1 also does, by sniffing
`'Error 500 (Server Error)'` out of an error page. That check is not ported;
its effect is.

**Exchange rates are memoised per `(from, to)` for the duration of one render.**
v1 issued one request per priced paragraph.

**URL buttons are deduplicated in first-appearance order.** v1 iterates
`set(re.findall(...))`, and Python randomises string hashing per interpreter, so
v1's ad buttons genuinely reorder after every restart. Same set of buttons,
deterministic order.

**The submission is not consumed until the fan-out commits.** v1's
`prepare_post` pops the cache entry as its last act, safe only because it could
not be retried. In an arq job it is not: a failure during the second Mural
upload would leave the retry with nothing to render, answering `publish_expired`
after the first caption had already posted. Read, then discard on success — so a
retry re-renders. Duplicate Mural posts are visible and recoverable; a silently
dropped campaign is neither.

**The publisher is inert until configured.** v1 hardcoded one deployment's Mural
and approval channels (`:20-22`). Unset ⇒ both commands answer
`publisher_unavailable`, rather than half-running a network with nowhere to
render into.

## Registration order — two hard constraints

Both are silent when wrong, so both are asserted in tests:

1. `publisher.relay_router` must sit **after** `groupguardian` and `complaint`
   and **before** `chat_ai`, which is where v1's `elif` is
   (`COOKIEBOT.py:302-303`). After `chat_ai`, the AI answers replies meant for
   a post's author.
2. `postgetter.router` must sit **before** `fun_random` — see that feature's
   contract.

## Phase 6 — parity table

| Aspect | Verdict |
|---|---|
| All six command spellings, the three callback tokens, every payload field order | **same** |
| Every user-visible string, all three languages | **same, verbatim** (`publish_queued`/`publish_queued_no_dm` stay English-only, as v1 has them) |
| `/divulgar`'s three precondition branches and their order | **same** |
| The five approval buttons, their labels and their day/NSFW payloads | **same** |
| Keyboard row order, the 5-row entity cap, the origin-link exclusion | **same** |
| Caption pipeline order and the 1020 truncation | **same** |
| Fan-out skip order | **same** |
| `/repost` day parsing, the 9999 default, the 10-17 window, 👍, HTML | **same** |
| Scheduler cadence (300 s) and the day-spend-before-send | **same** |
| Storage | **changed (intentional, fix)** — D-PF-1 |
| Approve-button authorisation | **changed (intentional, fix)** — D-PF-2 |
| Pending-post storage, and when it is consumed | **changed (intentional, fix)** — D-PF-3 |
| `max_posts` trim arithmetic | **changed (intentional, fix)** — D-PF-7 |
| Fate of a row after a transient delivery failure | **changed (intentional, fix)** — D-PF-8 |
| Members-only matching | **changed (intentional, fix)** — D-PF-12 |
| Translation provider | **changed (intentional)** — AGENTS.md §5; same contract, different vendor |
| URL button order | **changed (intentional)** — v1's order was per-process random |
| Exchange-rate call count | **changed (intentional)** — memoised per render |
| Hardcoded Mural/approval channels | **changed (intentional)** — configuration, and inert when unset |
| Where the render and fan-out run, and therefore when the report arrives | **changed (unavoidable consequence)** — same precedent `util_everyone`/`util_youtube` set |
| Document-as-animation, HTML on photo only, the same-currency discard, the day spent on a failed send | **same** — D-PF-4/5/6/9, preserved deliberately and asserted |

## Tests

| Layer | File |
|---|---|
| Unit — caption pipeline, keyboard, price conversion, media resolution | `packages/cb-core/tests/test_publisher.py` |
| Unit — triggers, the callback wire, `/repost` argument parsing | `packages/cb-gateway/tests/test_publisher_handlers.py` |
| Unit — rate memoisation, translation fallback, retry safety, the fan-out skip order, the delivery failure taxonomy, forum-topic routing | `packages/cb-worker/tests/test_publisher_job.py` |
| Integration — the repository against real Citus, the `max_posts` trim, cross-group cancel, `Task Count: 1`, colocation | `qa/integration/test_scheduled_posts.py` |
| Acceptance — the two QA scenarios plus six authored | `qa/features/util_postforwarder.feature`, `qa/test_util_postforwarder.py` |

## QA vs v1 conflicts recorded

1. Both QA scenarios read as though forwarding a post to the bot delivers it to
   group b. v1 requires an owner approval in between and delivers a day later,
   on a randomised schedule. The `Then` of each is unchanged; the approval press
   and "a day passes and the delivery sweep runs" are explicit `When` steps.
2. The approval workflow has no Gherkin anywhere in `../Cookiebot-QA` — only
   prose. Six scenarios are authored, not ported.
