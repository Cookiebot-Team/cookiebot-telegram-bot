# util_postforwarder — Design

Owns the schedule table, the cron, the pending-post cache and the settings that
`util_postgetter` and `util_deletereposts` both read. Those two reference this
document rather than restating it.

## R1 — data model

**R1.1 `scheduled_posts`** — new table, migration
`packages/cb-api/migrations/versions/0005_scheduled_posts.py`. Distributed on
`group_id`, `colocate_with => 'groups'` (AGENTS.md §4). `group_id` is the
**target** group — the one that receives the forward — because that is what the
hot path (the cron's per-group delivery, and `util_postgetter`'s consent check)
filters on.

```
scheduled_posts (
    group_id              bigint      NOT NULL REFERENCES groups(group_id) ON DELETE CASCADE,
    post_id               uuid        NOT NULL,          -- uuid7, app-generated
    origin_title          text        NOT NULL,          -- source channel title
    target_title          text        NOT NULL,          -- v1 baked both into `name`
    days_remaining        int         NOT NULL,
    next_run_at           timestamptz NOT NULL,
    source_chat_id        bigint      NOT NULL,          -- v1 postmail_chat_id
    source_message_id     bigint      NOT NULL,          -- v1 postmail_message_id
    requester_chat_id     bigint      NOT NULL,          -- v1 second_chatid
    requester_message_id  bigint      NOT NULL,          -- v1 second_messageid
    requester_user_id     bigint      NOT NULL,          -- v1 origin_userid
    created_at            timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (group_id, post_id)                      -- §4.3: shard key in the PK
)
```

**R1.2** Three indexes, one per query this feature actually issues:

| Index | Serves |
|---|---|
| `(group_id, next_run_at)` | the cron's due sweep, and `util_postgetter`'s per-group view |
| `(group_id, origin_title)` | `schedule_post`'s one-campaign-per-source-channel dedupe (v1's `name.split('-->')[0]`) |
| `(requester_chat_id)` | `util_deletereposts`' cancel, and the reply relay |

**R1.3 v1's `name` string is not ported.** It was simultaneously the primary
key, the source-channel match (`split('-->')[0]`), the target-group match
(`f"--> {title}" in name`) and the reply-relay match (`startswith(button_text)`).
Splitting it into `origin_title` + `target_title` + a `uuid7` PK turns three
substring scans into three indexed predicates and removes the collision that a
group titled `A --> B` would cause. `origin_title` keeps its exact v1 value so
the reply relay's `startswith` semantics survive (R6.2).

**R1.4 `requester_chat_id` is not the distribution column, on purpose.** The
cancel (`util_deletereposts`) and the reply relay both filter on it, and both
therefore fan out across shards. Both are rare, human-triggered, index-backed
single-table statements — not repartition joins — so §4.4 does not apply. Each
call site carries a comment saying so, per §4's "if you need one, say so".

## R2 — the pending-post cache (D-PF-3, D-PG-2)

**R2.1** `cb_core/pending_posts.py`: `put(key, PendingPost)`, `take(key)`
(read-and-delete, matching v1's `cache_posts.pop`), `get(key)`. Backed by
`cb_core.cache` with a TTL of `settings.publisher_pending_ttl_seconds`
(default 86400 — v1's dict had no expiry, but it also had no persistence, so a
day is strictly more generous than a process lifetime in practice).

**R2.2** Key is `publisher:pending:{forward_from_message_id}`, matching v1's
`str(msg['forward_from_message_id'])` keyspace exactly — including its
consequence, that two groups forwarding the same channel post share one cache
entry and the second overwrites the first.

**R2.3** `PendingPost` is a `msgspec.Struct` (`media_kind`, `file_id`,
`caption`, `caption_entity_urls: tuple[str, ...]`). Only the entity *URLs* are
kept, not whole entities: `prepare_post` reads nothing else off them (`:193-196`).

**R2.4** A cache miss at approval time is a real, now-visible failure mode that
v1 papered over with `KeyError` into the global traceback handler. The approval
job logs it, answers the callback with `publisher_expired` and writes no rows.

## R3 — reply path vs worker

Everything the group sees immediately stays in the gateway; everything that
touches Telegram more than once, or any third party, is a job (AGENTS.md §2.4).

| Step | Where |
|---|---|
| `/divulgar` validation + `publish_sent_for_approval` reply | gateway |
| forwarding to the approval chat + the `Approve post?` prompt | gateway — two Telegram calls into one fixed chat, not a fan-out |
| `prepare_post` (translate ×2, currency ×N, two media sends) | **worker**, `PUBLISHER_APPROVE` |
| the per-group fan-out that writes `scheduled_posts` | same job, after the render |
| the schedule report DMs | same job |
| `/repost` | gateway — one row, no external call |
| delivery | **worker cron**, `deliver_scheduled_posts` |
| reply relay | gateway — one lookup, one DM |

**R3.1** New job names in `cb_core/jobs.py`: `PUBLISHER_APPROVE =
"publisher_approve"`. The cron function needs no constant (it is never
enqueued by name from the gateway).

**R3.2** `cb_worker/jobs/publisher.py` holds both. It must not import from
`cb_gateway` (the rule `everyone.py`/`calladms.py` already follow): shared
pure logic — the caption pipeline, the keyboard builder, the currency parser —
lives in `cb_core/publisher.py` so both sides import downward only.

## R4 — the fan-out

**R4.1 The group list.** v1 iterated the Java backend's `registers` collection.
v2 reads one query in the worker:

```sql
SELECT g.group_id, g.title, gc.publisher_post, gc.sfw, gc.language,
       gc.publisher_members_only, gc.max_posts
FROM groups g LEFT JOIN group_configs gc USING (group_id)
WHERE g.left_at IS NULL
```

A cross-shard scan of a colocated join, in a scheduled worker job — §4.4's
sanctioned case, with the comment to say so. It replaces N per-group
`get_config` round trips **and** v1's per-group `getChat` call for the title,
which is why the title is denormalised into `groups`.

**R4.2 Skip order is v1's, exactly** (`:246-257`): no config row → skip;
`not publisher_post` → skip; `has_nsfw and sfw` → skip; `publisher_members_only`
and the author is not in that group's roster → skip. The roster check uses
`cb_core.members.roster(group_id)` and compares **usernames**, matching v1's
`origin_user['username'] not in str(members)` — with the substring accident
removed: v1 stringified the whole member list and ran a substring test, so an
author named `bob` matched a member named `bobby`. That is a bug, not a
behaviour; the port compares set membership. Recorded as D-PF-12.

**R4.3 The `max_posts` cap (D-PF-7).** v1's trim mutated the list it was
counting. v2 computes it as one statement per target group, before inserting:

```sql
DELETE FROM scheduled_posts
 WHERE group_id = $1
   AND post_id IN (SELECT post_id FROM scheduled_posts
                    WHERE group_id = $1 ORDER BY created_at LIMIT $2)
```

with `$2 = max(0, live_count + 1 - max_posts)` — evict the oldest campaigns so
that inserting this one leaves exactly `max_posts`. Single-shard, filtered on
`group_id`. When `max_posts` is its default `9999` this is always a no-op.

**R4.4 Schedule times.** `hour = randint(0, 23)`, `minute = randint(0, 59)`,
`next_run_at` = **tomorrow** at that time in the deployment's timezone —
`Instant.now().to_system_tz()`, then `.replace(hour=…, minute=…, second=0)`,
then `.add(days=1)`, matching `create_job`'s unconditional `timedelta(days=1)`
(`:96`). `whenever` is already the codebase's date library (`cb_worker/main.py`).

**R4.5 Report.** The header/line/footer strings are assembled verbatim per the
spec table and DM'd to `settings.owner_id` and to the requester, then the
requester's chat gets `publish_queued`. A `TelegramForbiddenError` on either DM
(v1's bare `except`) swaps the reply for `publish_queued_no_dm`; nothing else
in the job is caught by it, because v1's `try` only ever wrapped the reporting
block (`:277-286`).

## R5 — external calls

**R5.1 Translation goes through `cb_core.llm.router()`, not a new SDK.** A new
`translate` task in `DEFAULT_TASKS` (`provider="anthropic"`,
`model="claude-haiku-4-5"`, `max_tokens=2048`, `temperature=0.0`). Rationale:
v2 already owns a provider abstraction with per-tenant budget enforcement,
retries and a breaker (`x_conversational_ai`'s slice built it), and
`google-cloud-translate` would be a second way to reach a third party for one
call — AGENTS.md §5 forbids exactly that. The mechanism differs from v1's
Google Translate; the *contract* (a pt caption and an en caption, falling back
to the untranslated text on failure) is identical, and v1 already has that
fallback (`:206-209`). Recorded as an intentional divergence.

**R5.2** Failure of either translation ⇒ that caption is the untranslated
`emojis_to_numbers` output. This subsumes v1's `'Error 500 (Server Error)'`
string check, which existed only because its client returned an HTML error page
instead of raising; the check is not ported, its *effect* is.

**R5.3 Currency.** `httpx` GET to
`https://v6.exchangerate-api.com/v6/{key}/latest/{code}`, timeout
`settings.exchangerate_timeout_seconds` (default 10.0 — v1's own value).
Per-paragraph failure degrades to the unmodified paragraph, as v1 does. Rates
are memoised per `(code_from, code_target)` for the duration of one job so an
ad with eight priced paragraphs makes one request, not eight — v1 made one per
paragraph.

**R5.4 Price parsing.** `price-parser` is a new dependency (pure Python, one
module, no transitive deps). Hand-rolling `Price.fromstring`'s
symbol/amount/locale handling is how this port would silently change every
converted caption. It duplicates nothing already present.

**R5.5** No `CB_EXCHANGERATE_API_KEY`, no `CB_POSTMAIL_CHAT_ID`, or no
`CB_APPROVAL_CHAT_ID` ⇒ that half of the feature is inert: currency conversion
is skipped, and the two commands answer `publisher_unavailable`. A deployment
that has not opted into a publisher network must not half-run one.

## R6 — the remaining handlers

**R6.1 Approval authorisation (D-PF-2).** `yPub`/`nPub` are accepted only when
`callback.message.chat.id == settings.approval_chat_id`. Any other origin gets
`callback.answer()` and nothing else. `SendToApprovalPub` keeps v1's openness —
it is the ✔️ on the group's own prompt, pressed by group members by design.

**R6.2 Reply relay.** v1 matched `job['name'].startswith(button_text)` where
`button_text` is inline keyboard row 0 column 0 — the origin channel's title.
v2: `SELECT … WHERE origin_title = $1 ORDER BY created_at LIMIT 1`, a
cross-shard read (R1.4). Equality rather than `startswith` because `origin_title`
is now stored alone rather than as a prefix of a composite string; for every
row v1 could match, the two agree.

**R6.3 Registration order** in `handlers/__init__.py`. `publisher.router` joins
the command block (disjoint triggers). The reply-relay handler must sit exactly
where v1's `elif` does — after `groupguardian`'s captcha-reply check and
`complaint`'s reply check, **before** `chat_ai` — or a reply to a post would be
answered by the AI instead. That is the same ordering constraint
`handlers/__init__.py:96-102` already documents for `chat_ai`/`embedder`, so
the relay is registered as its own small router immediately before
`chat_ai.router`, not folded into the command block.

## R7 — the delivery cron

**R7.1** `deliver_scheduled_posts`, `cron(minute={0,5,10,…,55})` — v1's 300 s
tick, on the schedule shape `expire_captchas` already uses.

**R7.2** Per due row, in v1's order: decrement-or-delete first (D-PF-9
preserved), then re-read the target's `publisher_post` and delete the row when
it is off (D-PG-4 preserved), then forward from `source_chat_id`, passing
`message_thread_id` only when the target chat `is_forum` **and**
`thread_posts` is not NULL (D-PG-1 fixed — v2 already normalises the `"9999"`
sentinel).

**R7.3 Failure handling (D-PF-8 fixed).** `TelegramForbiddenError` (kicked) and
`TelegramBadRequest` naming a missing chat/message delete the row, as v1 does.
Anything else leaves the row alone and logs — the next tick retries. The
row is not immortal: `days_remaining` still ticks down every attempt (R7.2),
so a permanently broken target drains on its original schedule instead of
living forever.

**R7.4 Telemetry.** `cb_worker_publisher_delivery_total{outcome}` with outcome
in `sent|opted_out|dropped|error`, and
`cb_worker_publisher_fanout_total{outcome}` in `scheduled|skipped|error`. No
`group_id` label (AGENTS.md §7).

## Open decisions — answered

1. **One table, distributed on the target group.** R1.1 — the hot reader is
   delivery, which is per-target.
2. **Translation via the LLM router, not a Google SDK.** R5.1.
3. **`price-parser` is added; the parser is not hand-rolled.** R5.4.
4. **The approval press is authorised by chat id.** R6.1 — D-PF-2 is a real
   authorisation hole and this is the minimum that closes it without inventing
   a permission model v1 never had.
5. **Hardcoded chat ids become settings, and the feature is inert when they
   are unset.** R5.5 — the alternative, shipping one deployment's channel ids
   in a multi-tenant codebase, is not an option.
6. **The reply relay is its own router, placed by v1's `elif` position.** R6.3.
