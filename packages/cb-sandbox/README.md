# cb-sandbox

**A local Telegram your bot can actually talk to.**

It serves Telegram's own Bot API on one port and a control plane on another.
Point your bot's API base at it, tell it to long-poll, and every message you
send from the web client drives your real handler stack — same routers, same
middlewares, same database. Nothing about the bot is mocked or modified.

The thing it is for: a passing unit test tells you a handler *ran*. This tells
you **what a user sees**, and — through the API-call log — what the bot
actually asked Telegram to do, including the calls a chat window can never
show you: `deleteMessage`, `restrictChatMember`, `banChatMember`.

```
web client (:3001) ──REST + SSE──►  cb-sandbox (:8083)
                                      ├── /bot<token>/<method>   ← your bot polls this
                                      └── /api/...               ← the client and the test kit drive this
your bot (unchanged) ──API base = http://localhost:8083, long polling──►
```

---

## Quick start

```bash
pip install cb-sandbox
python -m granian --interface asgi --port 8083 cb_sandbox.app:app
```

Then point your bot at it. With aiogram:

```python
from aiogram.client.telegram import TelegramAPIServer
from aiogram.client.session.aiohttp import AiohttpSession

session = AiohttpSession(api=TelegramAPIServer.from_base("http://localhost:8083"))
bot = Bot(token="424242:SANDBOX", session=session)
# ...and start polling, not webhooks.
```

The token's numeric prefix must match the sandbox's configured bot id (default
`424242`). Most client libraries derive `bot.id` from that prefix without ever
calling `getMe`, so a mismatch makes "did *I* send this message?" answer
differently on the two sides of the same message.

With no configuration at all you get a working world: a group, the bot as its
administrator, a creator, a plain member, an admin with anonymity switched on,
and a private chat. That is enough to drive most of a group bot by hand.

---

## Making it *your* bot's sandbox

Drop a `sandbox.config.json` in your repository root. Everything in it is
optional; what you leave out keeps the built-in default.

```jsonc
{
  "bot": {
    "id": 424242,
    "username": "my_bot",
    "first_name": "My Bot",
    "can_read_all_group_messages": true
  },

  // Named starting worlds. One per situation worth reaching in one click.
  "seeds": [
    {
      "name": "default",
      "title": "Group with an anonymous admin",
      "users": [
        { "key": "alice", "first_name": "Alice", "username": "alice" },
        { "key": "carol", "first_name": "Carol", "username": "carol" }
      ],
      "chats": [
        {
          "key": "main",
          "title": "Test Group",
          "bot_role": "administrator",
          "members": [
            { "user": "alice", "role": "creator" },
            { "user": "carol", "role": "administrator", "anonymous": true }
          ]
        }
      ]
    }
  ],

  // What the bot does, as a validator would name it. This is the axis the
  // web client groups a whole test run by.
  "features": [
    {
      "id": "rules",
      "title": "Group rules",
      "status": "done",
      "commands": ["/rules"],
      "tags": ["regras"]        // scenario tags that also mean this feature
    }
  ],

  // The command palette. Generate it from your own parser if you can.
  "commands": [
    { "primary": "/rules", "aliases": ["/regras"], "feature_id": "rules", "status": "done" }
  ],

  // One click that puts a tester in front of a specific question.
  "presets": [
    {
      "id": "anon-admin",
      "button": "Anonymous admin sends a command",
      "seed": "default",
      "acting_user": "carol",
      "feature_id": "rules",
      "what_to_do": "Acting as Carol, send /rules.",
      "what_to_look_for": "It should be accepted — an anonymous admin arrives as GroupAnonymousBot."
    }
  ]
}
```

Discovery walks up from the working directory. `CB_SANDBOX_CONFIG=/path/to/file`
beats discovery and is what a process launcher or a test session should set —
discovery depends on the working directory, which a subprocess does not always
inherit the way its author assumed.

| Environment variable | What it overrides |
|---|---|
| `CB_SANDBOX_CONFIG` | The config file path (an explicit path that doesn't exist is an error, not a fallback) |
| `CB_SANDBOX_DB` | Where the DuckDB file lives (default `sandbox.duckdb`) |
| `CB_SANDBOX_BOT_ID` / `_BOT_USERNAME` / `_BOT_FIRST_NAME` | Identity, per run |
| `CB_SANDBOX_CORS_ORIGINS` | Extra browser origins allowed to drive `/api/...` |

`GET /healthz` reports which config it loaded and which bot it thinks it is —
the two facts behind almost every "the sandbox is running but the bot does
nothing" report.

---

## Seeing your tests by feature

A per-test result list answers *which check failed*. It cannot answer *is this
behaviour correct*, because that is a question about one feature and every
scenario that touched it — and it cannot answer *did we check this at all*,
because a feature nobody exercised has no row in a report of tests that ran.

So the sandbox records **scenarios** (a named span of activity; every message
and API call made while one is active carries its id) and files each one under
a **feature**. `GET /api/features` returns one row per declared feature with
the run folded in:

```jsonc
{
  "id": "rules",
  "title": "Group rules",
  "status": "done",
  "scenario_ids": ["test_rules.test_pt", "test_rules.test_en"],
  "scenario_count": 2,
  "status_counts": { "passed": 1, "failed": 1 },
  "message_count": 8,
  "api_call_count": 6
}
```

In the web client that becomes the top pane: features sorted so failures and
**untested** rows come first, each expanding into its own scenarios, each of
which filters the timeline and the API-call log down to just that check.

A scenario gets its feature from whichever comes first:

1. what the caller set (`"feature": "rules"` on `POST /api/scenarios`);
2. any of its **tags** matching a declared feature's `id`, `title` or `tags`.

The second rule is what lets an existing suite light up without being
rewritten — if it already tags its runs, it is already grouped.

---

## The test kit

Installing this package registers a pytest plugin. No conftest wiring:

```python
import pytest
from cb_sandbox.testkit import calls_to, wait_for


@pytest.mark.feature("rules")
def test_rules_answers(sandbox, sandbox_bot_id):
    chat = sandbox.create_chat("rules test")
    user = sandbox.create_user("Ana", "ana")
    sandbox.join(chat["id"], user["id"])
    sandbox.join(chat["id"], sandbox_bot_id)

    since = len(sandbox.state()["api_calls"])
    sandbox.send_message(chat["id"], user["id"], text="/rules")

    wait_for(
        lambda: next(iter(calls_to(sandbox.state(), "sendMessage", since)), None),
        timeout=10,
        description="answer /rules",
    )
```

| Fixture | What it gives you |
|---|---|
| `sandbox` | A `SandboxClient` — one method per `/api/...` route |
| `sandbox_base_url` | Where the sandbox is. **Override this** to point at a server your own suite starts |
| `sandbox_kit` / `sandbox_bot_id` / `sandbox_bot_username` | The configured bot, so no test hardcodes an id that lives in a config file |
| `sandbox_scenario` | Autouse. Opens one scenario per test and closes it with the test's real outcome |
| `sandbox_scenario_feature` / `sandbox_scenario_tags` | Override points for how *your* suite decides a feature, or adds a dimension (a locale, a backend) |

Options: `--sandbox-url` (use a server you started), `--sandbox-config`,
`--sandbox-db`, `--sandbox-keep-db`.

The scenario fixture is autouse so it is ordered first, which is why it can
claim the traffic a *fixture* generates while building the world. It skips
itself entirely for a test that never asks for a `sandbox` and carries no
`feature` marker, so a unit test in the same suite pays nothing.

When the run finishes you are left with a DuckDB file. Point a sandbox server
at it (`CB_SANDBOX_DB=… granian … cb_sandbox.app:app`), open the web client,
and read back what the suite actually did — filterable to one test, groupable
by feature.

---

## Images, stickers and files

Media carries **real bytes**, in both directions:

* attach a file in the web client and your handler receives it with real
  dimensions, a real size, and a mime type **sniffed from the bytes** rather
  than taken from the uploader's claim;
* when the *bot* uploads something — a generated captcha, a chart, a resized
  thumbnail — the sandbox keeps it and the web client renders it, which is the
  only way to validate an image feature at all;
* `getFile` and the file-download route serve the real content, so a handler
  that downloads what it was sent gets what it would get from Telegram.

Files are content-addressed (a `file_id` is derived from the SHA-256), so
re-attaching the same picture is the same file — which also matches the real
Telegram behaviour where a re-sent file keeps its `file_unique_id`.

A media message with **no** attached file is still legal and useful: six
stickers past a flood limit do not need to be six actual stickers. Those render
as a labelled placeholder, deliberately not as a broken image — a bot
re-sending a `file_id` minted by production is behaving correctly.

Limits: 8 MB per file, 128 MB per run. Reset clears the store.

---

## API surface

| Route | Purpose |
|---|---|
| `GET /healthz` | Readiness, plus the loaded config and bot identity |
| `GET /api/state` | The whole world: users, chats, messages, API calls, scenarios, feature rollup |
| `GET /api/kit` | What the bot *is* — identity, seeds, presets, commands, features |
| `GET /api/features` | One row per feature with this run's scenarios folded in |
| `GET /api/events` | SSE stream of world changes |
| `POST /api/seed` / `POST /api/reset` | Load a named world / reload the default |
| `POST /api/scenarios` (+ `/activate`, `/notes`, `/end`, `PATCH`) | Named spans of tagged activity |
| `POST /api/files` / `GET /api/files/{id}` | Upload and serve real media bytes |
| `POST /api/users` \| `/chats` \| `/users/{id}/dm` | Build a world |
| `POST /api/chats/{id}/join` \| `/leave` \| `/members/{uid}` | Membership |
| `POST /api/chats/{id}/messages` \| `/callback` | Send a message, press an inline button |
| `/bot<token>/<method>` | Telegram's Bot API, for the bot |
| `/file/bot<token>/<path>` | File downloads, for the bot |

---

## Bot API compatibility

The sandbox exists to make manual testing trustworthy: if it disagrees with
real `api.telegram.org`, a scenario that passes here proves nothing about
production. Every payload is validated against aiogram's real pydantic models
in `tests/test_telegram_api.py` — not eyeballed against the docs, because a
fake Bot API that is only eyeballed eventually certifies a broken bot.

### Semantics honoured

| Method | What it gets right |
|---|---|
| `getMe` | `can_join_groups`, `can_read_all_group_messages`, `supports_inline_queries` — from config, because handlers branch on them |
| `getUpdates` | Confirm-by-offset, negative offsets, `limit` bounds, `allowed_updates` (remembered across calls that omit it), and `409 Conflict` for a second concurrent poll |
| `sendMessage` / `editMessageText` | `parse_mode` genuinely parsed into plain `text` + `entities`; malformed markup raises the real `can't parse entities` error class; `reply_parameters`, `link_preview_options`, `message_thread_id`, `chat_id: "@username"` |
| Media sends | Captions parsed into `caption_entities` (a distinct field from `entities`); real width/height/mime from the stored bytes |
| `deleteMessage` / `deleteMessages` | Correct distinct error wording; partial success on a batch |
| `forwardMessage` / `copyMessage` | `from` is the bot, `forward_origin` carries the original attribution; `copyMessage` returns only `MessageId` and no attribution — the two real differences |
| `answerCallbackQuery` | Rejects an id never issued or already answered, with the real wording |
| `restrictChatMember` / `banChatMember` | `use_independent_chat_permissions` implications; `until_date` under 30s or over 366 days collapsing to "forever"; `getChatMember` renders the *actual* granted permissions |
| `setMyCommands` family | Stored and retrieved per `(scope, language_code)` with a fallback chain |
| Unknown method | `404 Not Found: method not found` — the real server's exact wording |

### Deliberate divergences

| Area | Real Telegram | Here | Why |
|---|---|---|---|
| Unimplemented method | A method-specific error | The same `404 method not found` as a genuinely unknown one | Telling them apart would mean hand-listing every Bot API method name. A 404 is never a silent 500, which is the property that matters |
| Rate limiting | `429` with `retry_after` | Never throttles | Simulating flood control would only slow a human down. The `parameters` envelope is still wired end to end |
| Group → supergroup migration | `migrate_to_chat_id` | Never happens | Nothing here converts a chat's type after creation |
| `deleteMessage` 48h window | Enforced | No time limit | Would make scenarios time-dependent for a rule with no testing payoff |
| Pinned messages | Several at once | Only the most recent | `getChat` also never renders `pinned_message`: it would embed a `Message` that embeds its own `chat`, recursing forever |
| `setMessageReaction` | Aggregates into `Message.reactions` | Recorded in the API-call log only | "Did the bot call it" is the question this tool answers |
| MarkdownV2 | Rejects unescaped reserved characters | Escapes only what it is asked to | Strict mode would make MarkdownV2 unusable for a human typing a test message by hand |
| Legacy `Markdown` | A distinct dialect | Routed through the MarkdownV2 parser | An approximation beats a third parser for a deprecated mode |
| Payments, inline mode, sticker management, business accounts | Full method families | Not implemented | No concept of an invoice or a sticker-set editor here; building the machinery would be speculation with nothing to validate against |
| Message/caption length, entity counts | Enforced | Not enforced | Input-validation edge cases with no testing payoff |

---

## How it is put together

```
src/cb_sandbox/
  config.py        what makes this *your* bot's sandbox — identity, seeds, features, commands
  state.py         users, chats, messages, files, the update queue — the shared world
  files.py         real media bytes, content-addressed, with mime/dimension sniffing
  telegram_api.py  /bot<token>/<method> — the surface your bot consumes
  control_api.py   /api/... — the surface the client and the test kit drive, plus SSE
  persistence.py   the durable DuckDB copy, so a run outlives the process
  testkit/         SandboxClient, SandboxProcess, and the pytest plugin
  app.py           assembly
```

State lives in memory for reads and in **DuckDB** for durability, so a run is
an artefact you can reopen. A failed write logs and carries on in memory: a
workbench that refuses to run because its notebook is locked is worse than one
that forgets a row.

One counter is special. `update_id` never goes backwards — not on reset, not on
restart — because a production bot almost always dedupes updates in a store
that outlives the sandbox process. If the counter rewound, that store would
treat the next batch as redeliveries and drop them before any handler ran, and
the bot would simply look dead with nothing in any log to explain why. It was
the single most confusing failure this tool could produce; see
`SandboxStore.next_update_id`.

---

## Licence

See `LICENSE` in the repository root.
