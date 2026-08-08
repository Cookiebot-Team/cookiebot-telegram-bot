# Database profile — what the session actually runs, and what it costs

Captured 2026-08-07 against the dev single-node Citus 13 (`citusdata/citus:13.0`
under podman, **amd64 emulated on Apple Silicon**, `CB_CITUS_SHARD_COUNT=8`).
Reproduce it with the recipe at the bottom.

**Read the absolute numbers as relative ones.** Everything database-shaped on
this host is roughly 20× slower than native (HANDOFF §8), and the fan-out costs
below are dominated by that. The *ratios* — coordinator overhead vs. shard work,
sequential scan vs. index scan — are what transfer to production.

## Method

1. `pg_stat_statements` (already in `shared_preload_libraries`) with
   `pg_stat_statements.track = all`, so Citus' per-shard statements are counted
   separately from the coordinator statement that fanned them out.
2. Workload: `pytest -m "not integration"` (2 772 tests, the full acceptance
   suite driving real handlers against real Citus) followed by the integration
   suite. 28 856 statements, 901 distinct, 5.6 s of total execution time.
3. `auto_explain` (`session_preload_libraries`, `log_min_duration = 0`,
   `log_analyze`, `log_buffers`, JSON) for a second pass that captured 920 real
   plans with actual row counts.
4. Targeted `EXPLAIN (ANALYZE, BUFFERS)` against **seeded volumes** — 200k
   users, 400k memberships, 100k scheduled posts, 2k groups — because the dev
   tables are empty and an empty table's plan proves nothing. The seed lived in
   a `-9.0e12` group-id band and was deleted afterwards.

`auto_explain` at `log_min_duration = 0` is not free: it made ten unrelated
tests fail on timing. It is a diagnostic setting, not a thing to leave on.

## Finding 1 — a partial index nothing could reach (fixed)

`users_birthday_idx` is `ON users (birth_month, birth_day) WHERE birthdate IS
NOT NULL` (migration `0001`). All three birthday reads filtered on
`birth_month`/`birth_day` and never mentioned `birthdate`, so Postgres could not
prove the index predicate held and refused to use it — every one of them
degraded to a parallel sequential scan of the whole `users` table.

`birth_month` is `GENERATED ALWAYS AS (EXTRACT(MONTH FROM birthdate))`, so
`birth_month = 8` *does* imply `birthdate IS NOT NULL`. The planner does not
reason through a generated column's expression to get there.

At 200k users:

| Query | Before | After | Plan |
|---|---|---|---|
| `all_users_with_birthday` (`/nextbirthday`, per command) | **318 ms** | **38 ms** | Parallel Seq Scan → Bitmap Index Scan |
| `groups_with_birthdays` (the daily sweep, all shards) | **681 ms** | **464 ms** | ditto, per shard |
| `members_with_birthday` (`/birthday`, one group) | — | — | already driven by `group_id`; the users side is a pkey probe |

Fixed in `cb_core/birthdays.py` by adding the redundant-looking
`birthdate IS NOT NULL` to all three statements — a semantic no-op that exists
only to unlock the index.
`qa/integration/test_birthday_broadcast.py::TestPartialIndexIsReachable` pins
it: it plans each statement with `enable_seqscan = off` (propagated to the
shards with `citus.propagate_set_commands = 'local'`) and asserts the index
name appears — plus a third test proving the same statement *without* the
predicate fails that assertion, so the test cannot pass vacuously.

**Look for the same shape before adding any partial index.** The other three in
the schema are fine: `group_members_joined_idx WHERE left_at IS NULL` and
`captcha_expiry_idx WHERE solved_at IS NULL` are both matched by their callers'
`WHERE`, and `users_username_idx WHERE username IS NOT NULL` is reachable from
`lower(u.username) = lower($1)` (verified in the plan).

## Finding 2 — cross-shard fan-out costs ~10–30× the work it dispatches

Measured, coordinator statement vs. the sum of its own shard statements:

| Statement | Coordinator | Per shard | Tasks |
|---|---|---|---|
| `scheduled_posts.delete_by_requester` (`/deleteposts`) | 85.9 ms | 0.2–0.9 ms | 8 |
| `birthdays.groups_with_birthdays` (daily) | 53.8 ms | 0.57 ms | 8 |
| `scheduled_posts.find_by_origin_title` (reply relay) | 9.3 ms | 0.26 ms | 8 |
| `llm.tenant_spend` (budget check) | 93.6 ms (first call) | 0.74 ms | 8 |

Every one of these is already justified in its own contract — the rows a
campaign owns are spread across every group it targeted, so no `group_id`
predicate is correct — and every one is index-backed on the shard. The cost is
the fan-out itself: planning plus a task per shard. On this emulated host that
is most of the wall clock; on a real multi-node cluster it becomes network
round trips, which is the same shape of cost.

Nothing to fix, two things to remember:

- **A cross-shard statement on the reply path is worth about ten single-shard
  ones.** `find_by_origin_title` runs on a reply to a published post; that is a
  human-triggered path and 9 ms is fine. A per-message one would not be.
- **`Task Count: 1` is the property the integration tests already assert**
  (`qa/integration/test_citus_topology.py`) for everything on the reply path.
  That assertion is the guard rail this finding argues for; keep adding it.

## Finding 3 — the hottest read is single-shard and warm

`group_config._SELECT` (`groups LEFT JOIN group_configs`, the read behind
`context_for`) is the most frequent production statement in the workload: 349
calls, **1.14 ms mean**, `Task Count: 1`, and both L1 and L2 caches in front of
it. Its first execution costs 31 ms of *planning* against 1.5 ms of execution —
Citus catalog introspection, which is exactly the cost HANDOFF §8 documents for
the first array-typed statement on a connection. asyncpg prepares statements per
connection, so this amortises; the 1.14 ms mean is the warm number.

## Finding 4 — `/ship` reads the whole membership to pick two names

`members._RANDOM_USERNAMES` is `ORDER BY random() LIMIT $2` over
`group_members JOIN users`. The plan is a bitmap scan of the group's whole
membership plus one `users` pkey probe per member, then a top-N sort — 100
members measured at 3.0 ms of shard work; a 10 000-member group is 10 000 index
probes to return two rows.

Not changed. It is single-shard, index-backed and correct, the alternatives
(sampling by offset, over-fetching then filtering) all change which members can
be drawn, and no group in the imported data is near that size. Worth revisiting
if one is: sample `group_members` first and join `users` afterwards.

## What was checked and is healthy

- **Topology.** 6 reference tables (`users`, `blacklist`, `bots`,
  `command_catalog`, `media_blobs`, `tenants`), 25 distributed on `group_id`,
  all in one colocation group. Every join in the workload stayed node-local.
- **Every reply-path statement** (`mediarestrict._joined_at`,
  `doomlist.check_local_blacklist`, `members.roster`, `members.count`,
  `captcha` reads and writes, giveaway lookups) is `Task Count: 1` and uses an
  index. No sequential scan on a distributed table on any per-message path.
- **`scheduled_posts_due_idx (group_id, next_run_at)`** does get used for the
  cron's `WHERE next_run_at <= now()` despite `group_id` leading — as a bitmap
  scan of the whole index, 1.9 ms per shard at 12.5k rows/shard. Fine now; a
  `(next_run_at)` index would turn it into a range scan if that table grows by
  an order of magnitude.
- **Connection churn**: 4 078 pool acquisitions in the run, each paying
  asyncpg's reset (`CLOSE ALL` + `pg_advisory_unlock_all()`) at 0.009 ms and
  0.013 ms. Noise.

## Reproducing

```bash
podman exec cookiebot-v2_citus_1 psql -U cookiebot -d cookiebot \
  -c "ALTER DATABASE cookiebot SET pg_stat_statements.track = 'all'" \
  -c "ALTER DATABASE cookiebot SET track_io_timing = on" \
  -c "SELECT pg_stat_statements_reset()"

python scripts/cb.py test && python scripts/cb.py test-integration

podman exec cookiebot-v2_citus_1 psql -U cookiebot -d cookiebot -P pager=off -c "
SELECT round(total_exec_time::numeric,1) ms, calls, round(mean_exec_time::numeric,3) mean_ms,
       left(regexp_replace(query,'\s+',' ','g'), 110) q
FROM pg_stat_statements ORDER BY total_exec_time DESC LIMIT 30"

# and reset what you set:
podman exec cookiebot-v2_citus_1 psql -U cookiebot -d cookiebot \
  -c "ALTER DATABASE cookiebot RESET pg_stat_statements.track" \
  -c "ALTER DATABASE cookiebot RESET track_io_timing"
```

For plans rather than totals, add `auto_explain` — but only for one run, and
expect timing-sensitive tests to fail while it is on:

```sql
ALTER DATABASE cookiebot SET session_preload_libraries = 'auto_explain';
ALTER DATABASE cookiebot SET auto_explain.log_min_duration = 0;
ALTER DATABASE cookiebot SET auto_explain.log_analyze = on;
ALTER DATABASE cookiebot SET auto_explain.log_nested_statements = on;
-- then: podman logs cookiebot-v2_citus_1
```
