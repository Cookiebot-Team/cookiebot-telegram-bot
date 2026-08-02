# Contract: fun_ship (v1 -> v2)

Phase 2/6 of `/migrate-feature` for `/shippar`, `/ship` and QA's `/shipp`
spelling. QA: `../Cookiebot-QA/features/fun_ship.feature`. FEATURE-MAP row:
`fun_ship`. Files owned by this port:
`packages/cb-core/src/cb_core/members.py`,
`packages/cb-gateway/src/cb_gateway/handlers/members.py`,
`packages/cb-gateway/src/cb_gateway/handlers/ship.py`,
`packages/cb-gateway/src/cb_gateway/handlers/__init__.py` (two router lines),
`qa/features/fun_ship.feature`, `qa/test_fun_ship.py`,
`packages/cb-gateway/tests/test_ship.py`, `packages/cb-core/tests/test_members.py`,
`qa/integration/test_members.py`, this file.

## The prerequisite this port had to build first

`/shippar` needs to know who is in the group, and **nothing in v2 recorded
that**. v1's answer was the `registers/{chat_id}` Mongo document, maintained by
`check_new_name` (`../COOKIEBOT-Telegram-Group-Bot/Bot/UserRegisters.py:64-88`)
on *every message*, before dispatch (`COOKIEBOT.py:118`). v2 had the tables
(`users`, `group_members`) and one writer for one of them
(`core_mediarestrict`'s join hook, which only fires on `new_chat_members`), so a
member who was already in the group when the bot arrived — the overwhelming
majority — existed nowhere.

So this port also lands `cb_core.members` + `cb_gateway.handlers.members`: the
registry, written on the same trigger v1 used. Three pending features read it
next (`util_everyone`, `util_birthday`, `util_nextbirthday`), which is why it is
a shared module rather than something private to this handler.

## Phase 2 — v1 behaviour contract

v1 handler: `shipp`, `../COOKIEBOT-Telegram-Group-Bot/Bot/UserRegisters.py:216-250`.
Dispatch: `../COOKIEBOT-Telegram-Group-Bot/Bot/COOKIEBOT.py:214-233`.

| Aspect | v1 behaviour (file:line) |
|---|---|
| Triggers | `/shippar`, `/ship` (`COOKIEBOT.py:232`, prefix match, so `@botname` and trailing arguments both still dispatch). QA spells it `/shipp`; already in `COMMAND_ALIASES`. |
| Preconditions | `funfunctions` (`COOKIEBOT.py:214-218`). No admin check, no reply requirement. |
| Cooldowns / quotas | None. |
| Argument handling | `if len(msg['text'].split()) >= 3:` -> `target_a, target_b = split()[1], split()[2]`, used **verbatim**, never resolved against the register. Fewer than three tokens (i.e. zero or one argument) -> both targets are drawn from the shuffled register and any lone argument is **discarded** (`:219-226`). |
| Success output | `i18n.get("ship", ...)` — EN: `"I detected a Couple! @%(target_a)s + @%(target_b)s = ❤️\n\nDynamics: %(ship_dynamic)s\nChildren: %(children_quantity)s 🧸\nChance of divorce: %(divorce_prob)s%% 📈"`. `ship_dynamic` is a random line of `ship_dynamics.txt`, `children_quantity` is `random.choice(['0','1','2','3'])`, `divorce_prob` is `str(random.randint(0, 100))`. Sent with `send_message(..., text, msg)` — 4th positional is `msg_to_reply`, so a **reply**. |
| Failure output | Fewer than two registered members -> `i18n.get("no_ship")` ("I haven't seen enough members to ship yet!"), also a reply (`:227-230`). Feature gated off -> `notify_fun_off` (`Miscellaneous.py:129-131`), also a reply. |
| Persistence | None by the command itself. The register it reads is written by `check_new_name` on every message. |
| Side effects | `react_to_message(msg, '❤️')` **first**, before the member lookup, so the heart lands even on the `no_ship` path (`:217`). `send_chat_action(..., 'typing')` — cosmetic, dropped like every other ported handler. |
| External calls | Backend `GET registers/{chat_id}` (`get_members_chat`, `:14-31`), process-cached. |
| Known defects | The register's membership test is `username not in str(members)` (`:84`) — a **substring** check against the list's repr, so `@bob` is considered already registered when `@bobcat` is in the list. D6-adjacent (v1's process-local caches); not in FEATURE-MAP's numbered list. Not reproduced. |

## QA vs. v1 conflict — the single tagged user

Upstream scenario 2 reads:

> **When** the user sends the command `/shipp @user1`
> **Then** the bot should reply with a shipp of user1 and another user in the group

v1 does not do this. `len(msg['text'].split()) >= 3` is `False` for two tokens,
so `@user1` never reaches `target_a`; both targets come from the random pool.
AGENTS.md §1 gives v1 observable behaviour and QA intent, so:

- the port ignores a lone argument (v1),
- `qa/features/fun_ship.feature` keeps the scenario name and trigger and states
  the real outcome, with the divergence in its header,
- `docs/site/content/docs/feature-map.mdx`'s `fun_ship` row records it.

Making the single-argument case work "as intended" is a one-line change that can
be made deliberately later; doing it silently inside a port would mean groups
seeing a different reply to a command they have used for years.

## Phase 6 — parity table

| Aspect | Verdict |
|---|---|
| Triggers (`/shippar`, `/ship`, `/shipp`, `@botname` forms) | **same** — asserted by a parametrised unit test |
| Gate (`functionsFun`) and its reply | **same** — `ctx.enabled("fun")`, `fun_off` text, replied not sent |
| Two explicit arguments used verbatim, unresolved | **same** — including shipping non-members |
| A lone argument is discarded | **same** — QA disagrees; see above |
| Typed `@` not stripped, so `/ship @a @b` renders `@@a` | **same** — cosmetic v1 quirk, preserved deliberately |
| Third and later arguments ignored | **same** |
| Random draws: dynamics line, children `0-3`, divorce `0-100` | **same** — `random`, not `secrets`, matching v1's distribution |
| Reply vs send | **same** — every branch replies |
| `❤️` reaction, before the lookup, on every path | **same** — best-effort, failure swallowed as in v1 |
| `no_ship` under two members | **same** |
| Localisation (en, pt, es) | **same** — the v1 catalog strings, unmodified |
| Member source | **changed (intentional)** — Postgres `group_members` ⋈ `users` instead of the Mongo register. Observably equivalent: both only know members who have spoken since the bot arrived. |
| Rename handling | **changed (bug fixed)** — v1 keyed the register on username, so a rename made the member vanish and re-register as a stranger. Keyed on `user_id` here. |
| Membership test | **changed (bug fixed)** — v1's `username not in str(members)` substring check silently skipped real members. A primary key cannot. |
| Leaving | **changed (intentional)** — `left_at` is stamped instead of the row being deleted, because `core_mediarestrict` measures member age from `joined_at`. Rejoining clears `left_at` and does **not** move `joined_at`. |
| Bots in the pool | **same** — v1 filtered nothing, so a chatty second bot can be shipped. `users.is_bot` is recorded for a future reader. |
| `typing` chat action | **changed (intentional)** — dropped, as in every prior port |

## Citus notes

- `random_usernames` is single-shard: `group_id` leads the predicate and `users`
  is a reference table, so the join is node-local (AGENTS.md §4.4).
- `users` **is** a reference table, so every write replicates to every node.
  `cb_core.members` keeps a process-local identity cache for exactly that reason
  — the same guard v1 had (`cache_users`, `UserRegisters.py:35`), for a different
  reason.
- The users upsert sets `updated_at = EXCLUDED.updated_at`, never `now()`: Citus
  rejects a non-IMMUTABLE function in `DO UPDATE SET`. This port hit that error
  for real against Citus 13 before the acceptance suite went green — the same
  trap HANDOFF.md §1 records for the rollups and `MediaService`.

## Tests

| Layer | File |
|---|---|
| Unit — triggers, argument rules, rendered text, random bounds | `packages/cb-gateway/tests/test_ship.py` |
| Unit — registry caching, degradation, SQL shape | `packages/cb-core/tests/test_members.py` |
| Integration — real Citus: rename, leave/rejoin, `joined_at`, per-group isolation | `qa/integration/test_members.py` |
| Acceptance — the Gherkin scenarios | `qa/features/fun_ship.feature`, `qa/test_fun_ship.py` |
