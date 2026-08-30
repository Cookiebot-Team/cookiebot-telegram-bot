# Contributing to Cookiebot

Cookiebot is built for furry community groups, largely by the people who run
them. **Reports and ideas are contributions.** You do not need to write Python
to make this bot better — the most useful thing this project gets is someone
saying "this command did the wrong thing in my group", because nobody here can
see your group.

## Report something

Every form asks for two or three facts and nothing more.

| | |
|---|---|
| [🐛 Something is broken](https://github.com/Cookiebot-Team/cookiebot-telegram-bot/issues/new?template=bug_report.yml) | A command, a moderation feature or a deployment behaving wrongly |
| [🔁 v2 behaves differently from v1](https://github.com/Cookiebot-Team/cookiebot-telegram-bot/issues/new?template=v1_parity.yml) | A command that changed its name, wording, permissions or reply |
| [💡 An idea or an improvement](https://github.com/Cookiebot-Team/cookiebot-telegram-bot/issues/new?template=idea.yml) | A new command, a better default, a rough edge |
| [📖 Docs wrong or missing](https://github.com/Cookiebot-Team/cookiebot-telegram-bot/issues/new?template=docs.yml) | A page that says something untrue |

Two things worth knowing before you file:

- **[Troubleshooting](https://cookiebot-team.github.io/cookiebot-telegram-bot/docs/using/troubleshooting)**
  covers the four reports that arrive most often — a silent bot, a `/config`
  menu that never appears, a setting that will not stick, and new members being
  removed. Checking it first is not a requirement; it is usually faster.
- **`/analysis`**, sent as a reply to the message that misbehaved, dumps exactly
  what the bot received. Pasting that into an issue is worth a paragraph of
  description. Remove anything private first — it is a raw Telegram payload.
- **Never file a security problem as a public issue.** Use
  [a private advisory](https://github.com/Cookiebot-Team/cookiebot-telegram-bot/security/advisories/new).

A half-formed idea is still worth filing. "This is annoying and I do not know
what would fix it" is a real issue: describing the annoyance is the part only
you can do.

## Suggest an improvement to a group's experience

If you run a community group, you see things nobody else does — a default that
is wrong for a 3,000-member chat, a refusal message that reads as rude in
Portuguese, a captcha timer that removes real people. Those reports change
defaults. File them as [ideas](https://github.com/Cookiebot-Team/cookiebot-telegram-bot/issues/new?template=idea.yml)
and say how big the group is; scale is usually the missing context.

## Change the code

Read [`AGENTS.md`](AGENTS.md) first. It is short, it is the rulebook, and it
applies to humans and to agents equally. The
[Development guide](https://cookiebot-team.github.io/cookiebot-telegram-bot/docs/development)
is the manual next to it.

```bash
git clone https://github.com/Cookiebot-Team/cookiebot-telegram-bot
cd cookiebot-telegram-bot
cp .env.example .env
python scripts/cb.py install     # uv sync --all-packages
python scripts/cb.py test        # the whole offline suite — no bot token needed
python scripts/cb.py setup       # …or stand the whole stack up and call it
```

`python scripts/cb.py --list` prints every task. There is no Makefile: CI runs
the same functions your terminal does.

### Test the HTTP API — no Telegram account needed

The Mini App and the web console talk to `cb-api`, and that surface is testable
on a laptop with nothing but `uv` and Docker or podman:

```bash
uv run scripts/qa_setup.py
```

One command: it checks your machine, writes a `.env` if you have none, starts
the database, applies the migrations, seeds a demo deployment, starts the API,
mints three tokens — an owner, a group admin and a stranger — and then calls
every endpoint and prints what each one answered next to what it was supposed
to answer. Run it again any time; it is idempotent.

You can log in without Telegram because the script signs its own `initData`
with a local-only bot token it puts in your `.env`. The signature is real and
verified the same way Telegram's is; what holding the key buys you is a session
**as anyone**, which is what makes the refusals testable — and the refusals are
most of this API's behaviour.

Then run the suite against it:

```bash
python scripts/cb.py api-test
```

Three layers under `qa/api/`, each answering a different question: **smoke**
(does a running deployment answer at all?), **contract** (does every response
match the shape `openapi.json` promises?) and **integration** (does it behave
correctly against real rows?). Each skips loudly, naming the command that would
fix it, rather than passing quietly when it tested nothing.

The [Testing the API](https://cookiebot-team.github.io/cookiebot-telegram-bot/docs/testing-the-api)
guide takes it from there, step by step: what the demo world contains, which
layer your case belongs in, a worked example at each, and the conventions the
suite follows. **Adding a test is a contribution**, and a negative one — a
caller who should be refused and is not — is worth more than most features.

### The one rule that matters most

**v1 compatibility is not negotiable.** Groups are using the old bot right now.
A command that changes its trigger, its permissions or its reply is a
regression, however much nicer the new code is. Where v2 does diverge on
purpose, the reason is written down in `docs/contracts/` — and a change that
diverges without that note will be asked for one.

### What a change looks like

1. **Branch.** `feat/<short-name>`, `fix/<short-name>`, `docs/<short-name>`.
2. **Write the scenario first.** New behaviour gets a scenario in
   `qa/features/`; a ported v1 feature gets the v1 behaviour captured before the
   implementation exists. A feature whose Gherkin is still red does not land.
3. **Implement**, following the layering in `AGENTS.md` — the reply path stays
   cheap, and anything slow (external APIs, image work) is a `cb-worker` job.
4. **Update the spec** if you finished a feature: flip its `status` in
   `scripts/spec.py`, then `python scripts/cb.py docs-sync`. Never hand-edit a
   generated frontmatter block.
5. **Update the docs** if a command, setting or reply changed. The user-facing
   pages live in `docs/site/content/docs/using/`.
6. **Run the gate.**

   ```bash
   python scripts/cb.py fmt      # ruff autofix + format
   python scripts/cb.py check    # lint, types, API docs, tests, benchmarks, spec consistency
   ```

   A change to the HTTP API has two more steps, both of which `check` will
   remind you about: `python scripts/cb.py api-lint` refuses an endpoint nobody
   documented, and `python scripts/cb.py api-docs` regenerates the published
   spec and the reference page.

7. **Open a pull request.** The template asks three questions; answer them
   briefly.

### Commit messages

[Conventional Commits](https://www.conventionalcommits.org/), because
`cliff.toml` builds the changelog from them and a release note is only as good
as the messages under it.

```
<type>(<scope>): <what changed, lowercase, no full stop>
```

- **type** — `feat`, `fix`, `perf`, `refactor`, `docs`, `test`, `build`, `ci`,
  `chore`.
- **scope** — the feature id when there is one (`fun_dice`, `x_giveaways`,
  `core_welcome`), otherwise the package or area (`cb-gateway`, `chart`,
  `site`). One scope per commit; if you need two, you probably want two commits.
- **subject** — what a reader of the changelog needs. `feat(fun_dice): /d<N>
  with a clamped repeat count` beats `feat(dice): improvements`.

A breaking change to observable behaviour is `!` plus a body explaining what a
group will notice: `feat(core_welcome)!: …`.

### Review

Every pull request gets read by a human, and the bar is behaviour, not style —
`ruff` already decided style, and it is not up for debate. Expect questions
about what a group would notice, about which layer a piece of work belongs in,
and about the scenario that proves it. None of that is a rejection; a first
contribution that needs two rounds is a normal first contribution.

## Translations

The bot speaks English, Portuguese and Spanish, and every string lives in
`packages/cb-core/src/cb_core/locale_data/`. A wording fix in any of the three
is a genuinely valuable, genuinely small pull request — a refusal message that
reads as rude is a bug like any other.

## License

By contributing you agree that your work is licensed under the
[Apache License 2.0](LICENSE), the same as the rest of the repository.
