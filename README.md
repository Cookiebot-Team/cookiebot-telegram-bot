<div align="center">

<img src="docs/assets/cookiebot.svg" width="112" alt="Cookiebot">

# 🍪 Cookiebot v2

**The Telegram group bot for furry community chats — rebuilt.**

Keeps chats free of spammers and raids, welcomes new members, runs giveaways and
conventions, talks back in natural language, transcribes voice notes, finds
sources for images, schedules cross-group posts, and hands out an unreasonable
number of memes.

</div>

---

## What Cookiebot is

Cookiebot has been running in furry community groups for years, under several
names — Cookiebot, Bombot, Pawsy, Tarinbot — one per community or convention.
It does three jobs at once:

🛡️ **Guards the chat.** A captcha on join, a shared doom-list of known bad
actors, sticker-flood limits, and a media hold on brand-new members. Raids stop
at the door instead of in the moderators' notifications.

🎉 **Runs the community.** Welcome messages, rules, birthdays, giveaways,
countdowns to Brasil Fur Fest / Pawstral / Furcamp / Fursmeet, and a publisher
that forwards approved posts between partnered groups.

😹 **Is fun to have around.** `/meme`, `/ship`, `/battle`, `/death`, `/dice`,
`/random`, reverse image search, music recognition from a voice note, and a bot
that answers when you talk to it.

Related projects: [the v1 bot](https://github.com/MekhyW/COOKIEBOT-Telegram-Group-Bot) ·
[backend](https://github.com/MekhyW/COOKIEBOT-backend) ·
[web hub](https://github.com/MekhyW/COOKIEBOT-WebHub) ·
[QA specs](https://github.com/MekhyW/Cookiebot-QA)

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

**Milestone M0 complete.** The foundation is in: three services, the database
schema, observability, storage, AI routing, the self-hosted API option, and a
test suite that runs the specs. One command is live end to end (`/isalive`);
the rest are being ported in order.

Live progress, measured from the spec and a real test run rather than written
by hand — the documentation site's **[progress board](https://cookiebot-team.github.io/cookiebot-telegram-bot/docs/progress)**:

```
features   ███████████░░░░░░░░░░░░░  25/53 done
v1 specs   ████████████░░░░░░░░░░░░  15/31 covered by an executable scenario
```

Regenerate it with `python scripts/cb.py docs-sync`; read it locally with
`python scripts/cb.py docs` (:3002).

Nothing is switched over yet — v1 keeps serving every group until a feature's
scenarios pass here.

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

## Documentation

Everything below lives in the documentation site (`docs/site`, Fumadocs) —
published at **https://cookiebot-team.github.io/cookiebot-telegram-bot**, or run locally with `python scripts/cb.py docs`.

| | |
|---|---|
| [Progress board](https://cookiebot-team.github.io/cookiebot-telegram-bot/docs/progress) | what's ported, what's next, which scenarios pass — all measured |
| [Features](https://cookiebot-team.github.io/cookiebot-telegram-bot/docs/features) | one page per feature: what it does, what must not change, whether it works yet |
| [Architecture](https://cookiebot-team.github.io/cookiebot-telegram-bot/docs/architecture) | how v2 is built and why |
| [Development](https://cookiebot-team.github.io/cookiebot-telegram-bot/docs/development) | setup, tasks, testing, the compiled hot path |
| [Sandbox](https://cookiebot-team.github.io/cookiebot-telegram-bot/docs/sandbox) | driving the real bot by hand against [telegram-sandbox](https://github.com/Cookiebot-Team/telegram-sandbox), the local Telegram we open-sourced out of this repo |
| [v1 feature map](https://cookiebot-team.github.io/cookiebot-telegram-bot/docs/feature-map) | every v1 feature traced to its source, with the known bugs |
| [Multi-tenant](https://cookiebot-team.github.io/cookiebot-telegram-bot/docs/multi-tenant) | running many bots on one core |
| [`HANDOFF.md`](HANDOFF.md) | where the last session stopped and what to pick up |
| [`AGENTS.md`](AGENTS.md) | rules for anyone (or anything) writing code here |

## Contributing

Read [`AGENTS.md`](AGENTS.md) first — it is short, and it is the rulebook.

The one rule that matters most: **v1 compatibility is not negotiable.** Groups
are using the old bot right now. A command that changes its name, its
permissions or its reply is a regression, however much nicer the new code is.

```bash
python scripts/cb.py check   # lint, tests, benchmarks, spec consistency
```

## License

[Apache License 2.0](LICENSE). v1 ships CC0 1.0; v2 moves to Apache-2.0 for the
explicit patent grant and the attribution requirement that a public-domain
dedication waives. Cookiebot is built by
[MekhyW](https://github.com/MekhyW) and contributors.
