<div align="center">

<img src="docs/assets/cookiebot-avatar.jpg" width="128" alt="Cookiebot" style="border-radius: 50%">

# 🍪 Cookiebot v2

**The Telegram group bot for furry community chats — rebuilt.**

Screens the people at the door, greets the ones who get in, holds the rules,
keeps sticker floods and drive-by spam out — then stays for the dice, the memes,
the giveaways, the birthdays and the conventions. In English, Portuguese and
Spanish, with Telegram as the whole interface.

[**Documentation**](https://cookiebot-team.github.io/cookiebot-telegram-bot) ·
[Set up a group](https://cookiebot-team.github.io/cookiebot-telegram-bot/docs/using) ·
[Every command](https://cookiebot-team.github.io/cookiebot-telegram-bot/docs/using/commands) ·
[Progress board](https://cookiebot-team.github.io/cookiebot-telegram-bot/docs/progress)

</div>

---

## What it does

| | | |
|---|---|---|
| 🛡️ **Guards the chat** | A captcha at the door with five attempts and an admin override, three block lists checked on join, a per-group sticker-flood limit, and a media hold on brand-new accounts. | [docs](https://cookiebot-team.github.io/cookiebot-telegram-bot/docs/using/moderation) |
| 👋 **Runs the room** | Welcome messages with nine placeholder spellings, the group's rules, three languages picked up from whoever added the bot, and a skin per event so a convention can run its own bot on the same core. | [docs](https://cookiebot-team.github.io/cookiebot-telegram-bot/docs/using/welcome) |
| 🎲 **Is fun to have around** | `/dice`, `/ship`, `/battle`, `/meme`, `/death`, `/random`, `/destroy`, `/unearth`, `/fortunecookie` — and per-group custom commands. | [docs](https://cookiebot-team.github.io/cookiebot-telegram-bot/docs/using/fun) |
| 🧰 **Does the chores** | Birthdays (including an unprompted daily post), `/adm` with a confirmation step, `/everyone`, YouTube search, and X / TikTok / Bluesky links rewritten so Telegram previews them. | [docs](https://cookiebot-team.github.io/cookiebot-telegram-bot/docs/using/utilities) |
| 🤖 **Talks and listens** | Answers when mentioned or replied to, transcribes voice notes, recognises music, reverse-searches images, and turns any unknown command into an image search — each with its own limits. | [docs](https://cookiebot-team.github.io/cookiebot-telegram-bot/docs/using/ai) |
| 🎁 **Runs the events** | Raffles drawn in the group with entry buttons and an admin-only end, countdown posters for the partnered conventions, and approved posts carried between partnered groups. | [docs](https://cookiebot-team.github.io/cookiebot-telegram-bot/docs/using/giveaways) |

**Every command answers to its Portuguese and Spanish spellings** — `/rules`,
`/regras` and `/reglas` are one command. The group's language changes what the
bot says back, never what it listens for.

Cookiebot has been running in furry community groups for years, under several
names — Cookiebot, Bombot, Pawsy, Tarinbot — one per community or convention.

Related projects: [the v1 bot](https://github.com/MekhyW/COOKIEBOT-Telegram-Group-Bot) ·
[backend](https://github.com/MekhyW/COOKIEBOT-backend) ·
[web hub](https://github.com/MekhyW/COOKIEBOT-WebHub) ·
[QA specs](https://github.com/MekhyW/Cookiebot-QA)

## Running it in your group

Add the bot, promote it to admin, and check it can see the chat:

```
/isalive
/config       → language, the two moderation timers, the feature switches
/newwelcome   → reply to the prompt with your greeting
/newrules     → reply to the prompt with your rules
```

Five minutes, start to finish:
**[Getting started](https://cookiebot-team.github.io/cookiebot-telegram-bot/docs/using)**.

## Why a rewrite

v1 works, and it has earned its retirement. It runs on a Telegram library that
was abandoned in 2018, as **five separate processes** — one per bot persona —
each with a 50-thread pool, each holding its own copy of the config that drifts
from the others, supervised by a script that kills the process whenever the host
crosses 70% CPU. The backend is a second language and a second database. Nobody
can answer "which group is costing us the most in AI calls", because nothing
counts.

v2 keeps every command and every behaviour, and changes what is underneath.

| | v1 | v2 |
|---|---|---|
| Telegram library | telepot (unmaintained since 2018) | aiogram 3, async |
| Processes | 5, one per persona, 50 threads each | 1, serving every persona; scale by replicas |
| Updates | long polling | webhook, or polling, or a self-hosted Bot API server |
| Backend | Java + MongoDB | Python + Postgres/Citus |
| State | Mongo + 2 SQLite files + a text file + per-process dicts | one database, shared cache |
| Media | re-uploaded per group, no dedupe | stored once, content-addressed, on GCS or S3 |
| AI | one hardcoded vendor, no accounting | any provider, per-task model choice, cost tracked per group |
| Failure handling | a script that reboots the VM | health checks, circuit breakers, retries |
| Tests | 63 written scenarios, none runnable | scenarios execute in CI against a mock Telegram |

## What's new for communities

**📊 Analytics that actually exist.** Every message, command, join and captcha is
recorded per group. Admins get real answers: which commands people use, when the
chat is busy, how many raids the captcha stopped, what the AI features cost.

**🤖 Bring your own AI.** The chat model is configuration, not code. Run the best
available model, a cheaper one, or a model on your own hardware — and see the
spend per group, per model, per day.

**🖼️ Media that doesn't duplicate.** The same sticker posted in fifty groups is
stored once. Groups get signed links instead of re-uploads, and a group that
leaves takes its media with it.

**🏷️ Proper multi-bot support.** Personas were five deployments held together by
a shell script. Now a bot brand is a row: its own owners, commands, branding,
language and AI budget, on shared infrastructure. Communities that need bespoke
commands get them without forking the bot.

**📎 Bigger files.** With a self-hosted Telegram Bot API server, uploads go from
50 MB to 2 GB and the per-bot rate limits disappear.

**🔒 Fewer sharp edges.** TLS verification is no longer disabled, health and
metrics endpoints are not public, writes are no longer silently swallowed by a
cache, and temporary files no longer collide between chats.

## Status

**Every feature in the spec is ported and green.** 65 of 65, with all 31 v1 QA
spec files covered by executable scenarios and the whole suite passing in CI.

Progress is measured from the spec and a real test run rather than written by
hand — the documentation site's
**[progress board](https://cookiebot-team.github.io/cookiebot-telegram-bot/docs/progress)**:

```
features   ████████████████████████  65/65 done
v1 specs   ████████████████████████  31/31 covered by an executable scenario
```

Regenerate it with `python scripts/cb.py docs-sync`; read it locally with
`python scripts/cb.py docs` (:3002).

## Try it

```bash
cp .env.example .env
python scripts/cb.py install     # dependencies
python scripts/cb.py up          # database, cache, dashboards (docker or podman)
python scripts/cb.py migrate     # create the schema (services also do this at startup)
python scripts/cb.py test        # the whole offline suite — no bot token needed
```

Then add `CB_BOT_TOKENS` to `.env` and run `python scripts/cb.py gateway`.

Dashboards land on <http://localhost:3000>.

Testing the HTTP API instead of the bot? One command does the whole thing —
database, schema, demo data, a running API, three tokens, and a table of what
every endpoint answered:

```bash
uv run scripts/qa_setup.py
```

No Telegram account or bot token needed; it signs its own `initData` with a
local-only key it writes to `.env`. Then:

```bash
python scripts/cb.py api-test    # smoke, contract and integration, over the HTTP API
```

[Testing the API](https://cookiebot-team.github.io/cookiebot-telegram-bot/docs/testing-the-api)
is the step-by-step guide.

## Repository Structure

```
cookiebot-telegram-bot/
├── deploy/                 # Deployment configurations (Docker & Helm charts)
├── docs/                   # Project documentation and feature contracts
│   ├── contracts/          # Per-feature behavior contracts referenced by tests
│   └── site/               # Fumadocs progress and documentation website
├── ops/                    # Observability setup (Grafana dashboards, Loki/Tempo scrape rules, OpenTelemetry)
├── packages/               # Core Python application packages (monorepo design)
│   ├── cb-api/             # FastAPI backend service & Alembic migrations (Citus DDL)
│   ├── cb-core/            # Shared runtime, main bot engine, and Cython-compiled hot modules
│   ├── cb-gateway/         # Webhook ingest server routing updates to the core bot
│   └── cb-worker/          # Background worker (arq jobs) handling slow or fan-out operations
├── qa/                     # Comprehensive testing suite (unit, integration, and BDD)
│   ├── e2e/                # End-to-end sandbox testing scenarios
│   ├── features/           # Gherkin (.feature) specifications defining bot requirements
│   └── integration/        # Database integration and Citus topology assertions
├── scripts/                # Development utility scripts and CLI tools
├── AGENTS.md               # Behavioral rules and repository shape for developers/agents
├── CONTRIBUTING.md         # Instructions on how to set up development, write features, and run checks
├── HANDOFF.md              # Current project status and progress notes
└── pyproject.toml          # Workspace configuration, dependencies, and linting rules
```

## Documentation

Everything below lives in the documentation site (`docs/site`, Fumadocs) —
published at **https://cookiebot-team.github.io/cookiebot-telegram-bot**, or run locally with `python scripts/cb.py docs`.

**For whoever runs a group**

| | |
|---|---|
| [Getting started](https://cookiebot-team.github.io/cookiebot-telegram-bot/docs/using) | add the bot, promote it, set a language, a welcome and the rules |
| [Commands](https://cookiebot-team.github.io/cookiebot-telegram-bot/docs/using/commands) | all 48, generated from the parser, with every spelling |
| [Configuring a group](https://cookiebot-team.github.io/cookiebot-telegram-bot/docs/using/configure) | every setting in `/config`, and the two most groups get wrong |
| [Moderation](https://cookiebot-team.github.io/cookiebot-telegram-bot/docs/using/moderation) | the captcha, sticker floods, the media hold, the block lists |
| [Privacy](https://cookiebot-team.github.io/cookiebot-telegram-bot/docs/using/privacy) | what is stored, what leaves the deployment, who is responsible |
| [Troubleshooting](https://cookiebot-team.github.io/cookiebot-telegram-bot/docs/using/troubleshooting) | silent bot, missing menu, a setting that will not stick |

**For whoever builds or runs it**

| | |
|---|---|
| [Progress board](https://cookiebot-team.github.io/cookiebot-telegram-bot/docs/progress) | what's ported, which scenarios pass — all measured |
| [Features](https://cookiebot-team.github.io/cookiebot-telegram-bot/docs/features) | one page per feature: what it does, what must not change |
| [Architecture](https://cookiebot-team.github.io/cookiebot-telegram-bot/docs/architecture) | how v2 is built and why |
| [Mini App API](https://cookiebot-team.github.io/cookiebot-telegram-bot/docs/miniapp-api) | the OAuth2 token flow a Telegram Mini App uses, the config and audit endpoints a group's admins get, and the fleet-wide reads its owners do |
| [API reference](https://cookiebot-team.github.io/cookiebot-telegram-bot/api-reference/) | every endpoint, generated from `openapi.json`: scopes, parameters, response fields, a copyable request |
| [Testing the API](https://cookiebot-team.github.io/cookiebot-telegram-bot/docs/testing-the-api) | step by step: stand it up, call it, and write the smoke, contract and integration tests |
| [Development](https://cookiebot-team.github.io/cookiebot-telegram-bot/docs/development) | setup, tasks, testing, the compiled hot path |
| [Sandbox](https://cookiebot-team.github.io/cookiebot-telegram-bot/docs/sandbox) | driving the real bot by hand against [telegram-sandbox](https://github.com/Cookiebot-Team/telegram-sandbox), the local Telegram we open-sourced out of this repo |
| [v1 feature map](https://cookiebot-team.github.io/cookiebot-telegram-bot/docs/feature-map) | every v1 feature traced to its source, with the known bugs |
| [Multi-tenant](https://cookiebot-team.github.io/cookiebot-telegram-bot/docs/multi-tenant) | running many bots on one core |
| [`HANDOFF.md`](HANDOFF.md) | where the last session stopped and what to pick up |
| [`AGENTS.md`](AGENTS.md) | rules for anyone (or anything) writing code here |

## Contributing

**Reports and ideas are contributions.** You do not need to write Python to
make this bot better — nobody here can see your group, so "this command did the
wrong thing" is information only you have. Every form asks for two or three
facts and nothing more:

| | |
|---|---|
| [🐛 Something is broken](https://github.com/Cookiebot-Team/cookiebot-telegram-bot/issues/new?template=bug_report.yml) | a command, a moderation feature or a deployment behaving wrongly |
| [🔁 v2 differs from v1](https://github.com/Cookiebot-Team/cookiebot-telegram-bot/issues/new?template=v1_parity.yml) | a command that changed its name, wording, permissions or reply |
| [💡 An idea or an improvement](https://github.com/Cookiebot-Team/cookiebot-telegram-bot/issues/new?template=idea.yml) | a new command, a better default, a rough edge worth smoothing |
| [📖 Docs wrong or missing](https://github.com/Cookiebot-Team/cookiebot-telegram-bot/issues/new?template=docs.yml) | a page that says something untrue |

Half-formed ideas are welcome. Security problems are not — report those
[privately](https://github.com/Cookiebot-Team/cookiebot-telegram-bot/security/advisories/new),
never in a public issue.

Writing code? [`CONTRIBUTING.md`](CONTRIBUTING.md) has the whole loop — branch,
scenario, implementation, gate, pull request — and [`AGENTS.md`](AGENTS.md) is
the rulebook it follows.

The one rule that matters most: **v1 compatibility is not negotiable.** Groups
are using the old bot right now. A command that changes its name, its
permissions or its reply is a regression, however much nicer the new code is.

```bash
python scripts/cb.py fmt     # ruff autofix + format
python scripts/cb.py check   # lint, types, tests, benchmarks, spec consistency
```

## License

[Apache License 2.0](LICENSE). v1 ships CC0 1.0; v2 moves to Apache-2.0 for the
explicit patent grant and the attribution requirement that a public-domain
dedication waives. Cookiebot is built by
[MekhyW](https://github.com/MekhyW) and contributors.

The avatar and the colour palette are shared with the
[web hub](https://github.com/MekhyW/COOKIEBOT-WebHub) — one bot, one face,
one set of colours across everything it ships.
