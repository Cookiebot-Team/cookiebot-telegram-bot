# The real end-to-end suite (`qa/e2e/`)

`docs/SANDBOX.md` describes a workbench a person drives by hand. `CB_QA_SANDBOX=1`
(`qa/sandbox_harness.py`) drives the *acceptance* suite through `cb_sandbox`'s
store, but it feeds updates straight into the aiogram dispatcher in-process and
only mirrors them into the sandbox — `cb_sandbox.control_api` is never called
and `getUpdates` polling never runs. Neither one drives the gateway through
HTTP end to end.

`qa/e2e/` closes that gap. It starts the real, unmodified `cb_sandbox.app:app`
and `cb_gateway.main:app` as two subprocesses, wired exactly as
`docs/SANDBOX.md` describes (`CB_TELEGRAM_API_BASE` + `CB_TELEGRAM_INGEST=polling`),
and drives every scenario purely by calling the sandbox's `/api/...` control
surface over real HTTP — the same surface a human clicks through in the web
UI. Every assertion reads back `GET /api/state`: the chat transcript and,
more importantly, the `api_calls` log — the only place `deleteMessage`,
`restrictChatMember`, `banChatMember` and `answerCallbackQuery` are visible at
all.

## How this differs from the other two suites

| | `cb.py test` (`qa/*.py`) | `CB_QA_SANDBOX=1` | `cb.py test-e2e` (`qa/e2e/`) |
|---|---|---|---|
| Telegram fake | `qa/mock_telegram.py`, in-process | `cb_sandbox`, in-process | `cb_sandbox`, a real subprocess on a real port |
| Gateway | `cb_gateway.main.dp.feed_update()` called directly | same — `feed_update()` called directly | a real `cb_gateway.main:app` subprocess, actually polling |
| Transport | none (Python call) | none (Python call); the update is only *mirrored* into the store | real HTTP both ways |
| Infra | none required | none required | Postgres + Valkey, already up (`cb.py up`) |
| Speed | fast (the whole suite) | fast | slow (process startup, real polling) |
| Question it answers | "is the behaviour still correct" | "what does that behaviour look like" | "does the shipped gateway, talking real HTTP to a real Bot-API-shaped server, actually do the right thing" |

None of the three replace either of the other two. `cb.py test` is the CI
gate and must never slow down or flake because this suite exists — see
"Why it's opt-in" below.

## Running it

```bash
python scripts/cb.py up        # Postgres + Valkey (podman/docker compose)
python scripts/cb.py test-e2e
```

That's it — the task itself sets `CB_RUN_E2E=1`, starts a sandbox and a
gateway subprocess per test session, and tears both down afterward. A plain
`python scripts/cb.py test` (or `pytest qa/e2e` with no marker) skips every
test in this package instantly; see "Why it's opt-in" below.

Environment overrides (rarely needed):

| Variable | Default | Purpose |
|---|---|---|
| `CB_E2E_PG_DSN` (falls back to `CB_PG_DSN`) | `postgresql://cookiebot:cookiebot@localhost:5432/cookiebot` | the shared Postgres this suite reads/writes `groups`/`group_configs` rows in |
| `CB_E2E_REDIS_DSN` | `redis://localhost:6379/14` | a Valkey database index of its own — never 0 (dev), never 15 (`qa/`'s own tests) |
| `CB_E2E_SANDBOX_DB` | `sandbox-e2e.duckdb` at the repo root | where the run is recorded, so it can be opened afterwards — see "Reading the run afterwards" |

If Postgres or Valkey is unreachable, the suite skips cleanly (`pytest.skip`,
same pattern as `qa/conftest.py`'s `database`/`valkey` fixtures) — it never
fails just because infra isn't up.

## What it needs, and what it does per run

1. **Two subprocesses.** `cb_sandbox.app:app` on a free port with its own
   throwaway DuckDB file (never the developer's `sandbox.duckdb`), and the
   real `cb_gateway.main:app` on another free port, pointed at the sandbox
   (`CB_TELEGRAM_API_BASE`) with `CB_TELEGRAM_INGEST=polling`. Readiness is
   polled (`GET /healthz`, `GET /readyz`), never a fixed sleep; a process that
   dies before becoming ready fails loudly with its own log tail.
2. **A one-time database warm-up.** The first array-typed query on a fresh
   asyncpg connection pays a real, multi-second Citus catalog-introspection
   cost on an emulated container (the same cost `qa/conftest.py`'s `database`
   fixture works around with a 60s command timeout). `qa/e2e/conftest.py`'s
   `gateway_process` fixture pays it once, in setup, with a generous timeout
   and a throwaway group, so every real scenario's own `wait_for` budgets for
   what it is actually testing.
3. **One fresh group per test.** A sandbox chat via `POST /api/chats`, backed
   by a `groups` row in the *shared* Postgres (every handler's config/rules/
   admin/captcha lookups need it — every dependent table has
   `FOREIGN KEY (group_id) REFERENCES groups (group_id) ON DELETE CASCADE`).
   Titled `e2e:<test node id>` so a human can tell which test owns which row.
   Postgres is shared across concurrent runs and CI workers in a way the
   sandbox's own throwaway DuckDB file is not, so cleanup is real:
   `DELETE FROM groups` (cascades everything) runs in a `finally`, and setup
   defensively deletes any same-id leftover from an earlier, killed run first
   (the sandbox's chat-id counter restarts at the same values every session;
   Postgres does not).
4. **Bounded polling, never a blind sleep.** `qa/e2e/client.py:wait_for`
   polls `GET /api/state` on a short interval up to an explicit timeout and
   raises with the tail of `api_calls` on failure — "the bot never answered X
   within Ns; last api_calls: [...]" — never `sleep(3)` and hope.

## Why it's opt-in

`scripts/cb.py test` runs `pytest -q -m "not integration"` with no path
argument, so `qa/e2e/`'s modules get collected by `pyproject.toml`'s
`testpaths` like everything else. `qa/e2e/conftest.py`'s
`pytest_collection_modifyitems` hook skips every item carrying
`pytest.mark.e2e` unless `CB_RUN_E2E=1` is set — which only
`scripts/cb.py test-e2e` does. That keeps the fast gate fast: collection still
happens (cheap: no subprocess, no network), but every fixture is skipped
before it can run, so `cb.py test`/`cb.py test-all`/`cb.py check` never pay
for a subprocess, let alone two, on this suite's account.

## Reading the run afterwards

The suite is not only a pass/fail gate — it is a recording. Every test opens a
**scenario** in the sandbox before its first action and closes it with the
test's own outcome, and every message and Bot API call in between is tagged
with it. So when the run finishes you can open the whole thing in the web UI
and step through what the bot actually did, one test at a time:

```bash
CB_SANDBOX_DB=sandbox-e2e.duckdb python scripts/cb.py sandbox   # :8083
python scripts/cb.py sandbox-web                                # :3001
```

Then pick a test from the scenario filter. You get that test's chat traffic,
that test's API calls (`deleteMessage`, `restrictChatMember`, `banChatMember`
— the ones no chat window shows), and what the test was for: its name, its
docstring, its file, the group id it owned, and, when it failed, the traceback
as a note.

No gateway is needed to read a recording — the bot is not going to answer
anything, the answers are already in the file. Start one only if you want to
keep driving the world by hand from where the suite left off.

The file is wiped at the *start* of each run, not the end: a recording that
deleted itself on exit would be missing exactly when you want it. The run's
path is printed in the pytest summary.

What each scenario carries:

| Field | From |
|---|---|
| name | the test function's name |
| description | its docstring |
| tags | its module, minus the `test_` prefix (`captcha`, `rules`, ...) |
| metadata | `nodeid`, source `file`, pytest `markers`, and the `group_id` the test owned |
| status | `passed` / `failed` / `skipped`, including a failure during fixture setup |
| notes | the failure traceback, when there is one |

A person doing manual UAT can open a scenario the same way from the UI, and
record what they were checking and what they concluded next to the bot's own
behaviour.

## Scenarios covered

Picked from what `docs/MIGRATION-STATUS.md` marks done and green, one file
per feature area. Every scenario below runs **twice** — once for an `en`
group, once for a `pt` group — because `qa/e2e/conftest.py`'s `lang` fixture
is parametrized and `group_id`/`captcha_group_id` both depend on it, so any
test using either is pulled into the same parametrization automatically.
`en` was the only language this suite ever exercised until that fixture
existed, even though `pt` groups are the majority of the bot's real traffic;
`test_x[en]`/`test_x[pt]` node ids (and matching sandbox scenario tags) are
how the two runs stay distinguishable, in pytest's own output and in the
recording:

| File | Scenario |
|---|---|
| `test_privacy_and_commands.py` | `/privacy` and `/commands` round trip |
| `test_rules.py` | `/rules` with none configured; the `/newrules` two-step reply flow, admin and non-admin |
| `test_config_menu.py` | `/configurar` denies a non-admin (+ tutorial video); an anonymous admin is *not* denied — the v1 defect the port fixes; an admin who opened a DM gets the menu there and its buttons answer |
| `test_captcha.py` | a newcomer is challenged, the challenge *replies to the join message*, and the approve button answers |
| `test_calladms.py` | `/adm` -> press the confirm button via `/api/chats/{id}/callback` -> `answerCallbackQuery` + `deleteMessage` + the admin ping |
| `test_join_chain.py` | a plain self-join falls through to the welcome message; a doomlisted name is banned before welcome ever runs |
| `test_stickerspam.py` | sticker flood warns at the limit, deletes past it |
| `test_mediarestrict.py` | a fresh member's photo is deleted within the restriction window; an admin's never is |

Not every string a test touches changes with the group's language, and each
file says why inline where it matters: `rules.py`'s `/newrules` prompt and its
two outcome texts are hardcoded English in v1 too (`Configurations.py:271,
278,283`) and stay English in both language runs on purpose;
`config_menu.py`'s denial and can't-DM-you texts, by contrast, *are*
hand-translated per language (`_DENIED_TEXT`/`_CANNOT_DM_TEXT`) and are
asserted in the group's own language, not pinned to English. Command triggers
sent are the real per-language alias from `cb_core.textmatch.COMMAND_ALIASES`
(`/regras` in a `pt` group, `/rules` in an `en` one) wherever one exists;
`/adm` and `/isalive`'s hardcoded-English reply are noted as exceptions where
they occur.

Not covered, and why:

- Anything not yet ported (`docs/MIGRATION-STATUS.md`'s "planned"/"not
  ported" rows) — there is nothing to drive.
- `es`: a real v1 language, but out of scope for the task that added `pt` —
  `qa/e2e/conftest.py`'s `_LANGUAGES` tuple is the one line to extend later.

## Two sandbox defects this suite found, and their fixes

Both were real, both blocked coverage, and both are now fixed in
`cb_sandbox` — recorded here because each has a matching regression test and
because the failure modes are worth recognising again.

1. **`join_chat` did not persist the join's own service message.**
   `POST /api/chats/{id}/join` queued a `new_chat_members` update but never
   called `SandboxStore.add_message` for it. Most of the join chain never
   noticed (`welcome`/`doomlist`/`mediarestrict` do not `.reply()` to it), but
   `groupguardian`'s captcha issuance does — `message.reply(text,
   reply_markup=keyboard)` — and `cb_sandbox` validates reply targets for
   real, so every self-join with captcha on came back `400 Bad Request:
   message to reply not found` and the challenge was never issued. The
   captcha looked broken; nothing about the captcha was broken.
   Joins and leaves are now stored messages carrying a `service` field, the
   way Telegram models them. `qa/e2e/test_captcha.py` asserts the challenge's
   `reply_to_message_id` points at the join message, which is the assertion
   that would have caught it.
2. **No DM could be addressed by a user's own id.** Real Telegram identifies
   a private chat by `chat.id == user.id`, and `config_menu.open_config_menu`
   calls `bot.send_message(ctx.actor.user_id, ...)` on exactly that
   assumption. Every chat the control API minted took an id from the
   descending group-chat counter, so no handler answering privately could
   ever reach one. `POST /api/users/{id}/dm` now creates the chat whose id is
   the user's id — "this user pressed Start" — and a bot sending to a user
   with no DM gets Telegram's real `403 Forbidden: bot can't initiate
   conversation with a user` rather than a misleading "chat not found".
   `test_config_menu.py` covers both branches.

## A real gateway behaviour running `pt` groups surfaced (v1 parity, not a bug)

Adding a `pt` group to this suite ran straight into
`cb_gateway/handlers/setlang.py`'s `on_bot_added_to_group`: every time the bot
is (re-)added to a group, it derives that group's language from
`language_code` on whoever performed the add and overwrites `group_configs
.language` with it — unconditionally, via `group_config.set_config`. This is
`COOKIEBOT.py:121-135`'s `set_language` ported verbatim, not a v2 regression:
on real Telegram the adder is always a human admin, so this is a legitimate
"detect the language from whoever just invited me" heuristic, faithful to v1.

`qa/e2e/conftest.py`'s `_make_group` has no admin user to attribute the bot's
join to at the point it happens, so it self-joins the bot with no
`by_user_id` — a shape real Telegram cannot produce, but `cb_sandbox` allows.
That makes the join's `from_user` resolve to the bot's *own* sandbox record,
whose `language_code` defaults to `"en"` and derives to the literal `"eng"` —
silently overwriting whatever language `_make_group` had just configured, on
every single group this suite creates, regardless of which language was
asked for. Undetected until now because it happened to coincide with the only
language ever tested (`"eng"` and `"en"` both resolve to the same canonical
`en`); a `pt` group failed loudly the instant it existed, with the button
label `"ADMINS: Approve"` where `"ADMINS: Aprovar"` was expected — as clean a
demonstration as this suite could ask for of exactly the gap it was built to
close.

Fixed inside the fixture, not the handler (out of this task's file
ownership, and there is nothing to fix in the handler — it is working as
designed): `_reassert_group_language` waits for the `setMyCommands` call that
always follows `setlang`'s overwrite, then corrects `group_configs.language`
back to what the test asked for and invalidates the gateway's cache for it
the same way `group_config.invalidate` does (delete the L2 key, publish on
`cb_core.cache.INVALIDATION_CHANNEL`) — a plain SQL `UPDATE` alone would not
be seen by the gateway's own 30s in-process cache. See its docstring in
`qa/e2e/conftest.py` for the full mechanics.

## Troubleshooting

- **A test times out with a long `api_calls` tail in the message**: that tail
  is the point — read it. It shows every Bot API call the gateway actually
  made since the assertion's own starting offset.
- **Every test in a run fails/errors the same way at startup**: check
  `_wait_ready`'s exception — it includes the tail of the failing process's
  own log file (path printed in the error). Both processes' logs are written
  unbuffered (`PYTHONUNBUFFERED=1`) specifically so a failure while the
  process is still up shows something.
- **A UniqueViolation on `group_configs`/`groups` during fixture setup**:
  means a previous run of this suite was killed hard enough to skip its own
  `finally` (a `pytest --pdb` session left open, a `SIGKILL`, ...). The
  fixture self-heals (`DELETE FROM groups WHERE group_id = ...` before every
  insert) — if you see this, it means the self-heal itself needs to run
  again, which happens automatically on the next attempt.
- **The very first test after a cold start occasionally takes 30-90s instead
  of a few seconds**: this is the database warm-up (see above) absorbing a
  real, occasionally-severe hiccup — this environment has been observed to
  drop the gateway's very first connection to the sandbox, which aiogram's
  own polling loop then retries with growing backoff until it clears. It is
  not a bug in the code under test; `_warm_up_database_pool`'s docstring has
  the full explanation.
