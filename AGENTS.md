# AGENTS.md — Cookiebot v2

Rules for anyone (human or agent) writing code in this repo. Read this before the
first edit. Longer context: `docs/site/content/docs/architecture.mdx`, `docs/site/content/docs/feature-map.mdx`.

The single hardest constraint: **v2 must be backwards compatible with v1.** Groups
are live on the old bot. A feature that "works" but changes a command name, a
reply shape, or a config default is a regression, not a port.

---

## 1. Repository shape

```
packages/cb-core/      shared runtime; the four Cython hot modules live here
packages/cb-api/       FastAPI service + alembic migrations (all Citus DDL)
packages/cb-gateway/   aiogram webhook ingest — no business logic beyond routing
packages/cb-worker/    arq jobs and cron — anything slow or fan-out
qa/                    executable acceptance suite (pytest-bdd + mock Telegram)
ops/                   otel collector, prometheus, tempo, grafana
docs/site/             the documentation + progress site (Fumadocs, published to Pages)
docs/contracts/        per-feature behaviour contracts, referenced by the tests
```

Reference repos, read-only, one directory up:

- `../COOKIEBOT-Telegram-Group-Bot` — v1 bot (Python, telepot). **The behavioural source of truth.**
- `../COOKIEBOT-backend` — v1 backend (Java/Spring, MongoDB). Source of truth for stored shapes.
- `../Cookiebot-QA` — Gherkin specs. Source of truth for intended behaviour.

Where the three disagree, v1 code wins for *observable behaviour* and QA wins for
*intent*. Record the conflict in `docs/site/content/docs/feature-map.mdx` rather than silently picking.

## 2. Non-negotiables

1. **No new command name without an alias.** Every v1 trigger keeps working. New
   canonical names go in `cb_core/textmatch.py:COMMAND_ALIASES` next to the old
   one, never instead of it.
2. **Every query filters on the distribution column.** `group_id` for distributed
   tables. A query without it fans out to every shard. See §4.
3. **UUIDv7 for surrogate keys**, generated in the app (`cb_core.ids.uuid7`). Never
   `uuid4`, never a database sequence.
4. **Nothing slow on the reply path.** ffmpeg, image compositing, LLM calls,
   multi-chat fan-out → enqueue to `cb-worker`. The gateway answers Telegram fast
   or Telegram redelivers.
5. **No secrets or per-request values in a cached prompt prefix**, no credentials
   in code, no `verify=False` (v1 shipped it — FEATURE-MAP D2).
6. **Analytics never breaks a reply.** `EventRecorder` and `llm_usage` writes
   swallow their own errors and count the failure.
7. **Test pyramid, in this order** — see §6. A feature is not done without unit
   tests, a DB integration test, and its Gherkin scenario passing.

## 3. Adding or porting a feature

Use the skills; they encode this checklist:

| Task | Skill |
|---|---|
| New feature with no v1 equivalent | `/implement-feature` |
| Porting a v1 feature (the common case) | `/migrate-feature` |
| Reviewing a diff | `/review-changes` |
| Lint / format / type / gate | `/lint-code` |

The order for a port is always: read v1 source → extract observable behaviour →
find the QA scenario → write the failing test → implement → verify parity.

When it lands, flip the row's `status` in `scripts/spec.py` and run
`python scripts/cb.py docs-sync`. That regenerates the site's progress data and
the feature page's **frontmatter** — never its prose, which is where you write
what the feature does and what must not change. Do not hand-edit a `status:`
in an `.mdx`: `cb.py check` runs `docs-sync --check` and fails on the drift.

## 4. Postgres and Citus rules

**Distribution.** `group_id` is the shard key for everything tenant-scoped, and
every such table is created with `colocate_with => 'groups'`. Colocation is what
makes a per-group join node-local. Breaking it turns a router query into a
repartition join.

**Table type decision:**

| Data | Type | Why |
|---|---|---|
| Tenant-scoped rows (configs, members, media, usage) | distributed on `group_id`, colocated with `groups` | single-shard reads and writes |
| Small, joined from everywhere (`users`, `blacklist`, `bots`, `command_catalog`, `media_blobs`) | reference table | replicated, so joins are node-local |
| Migration bookkeeping | local (coordinator) | never joined |

**Minimising block exchange** — the actual rules, in priority order:

1. Put `group_id` in the `WHERE` clause of every hot query. One shard, no exchange.
2. Put `group_id` first in every composite index and in every `GROUP BY` that
   feeds a rollup, so aggregation is per-shard and only the small result moves.
3. Include `group_id` in every primary key, unique constraint and foreign key on
   a distributed table. Citus requires it, and it forces the tenant-scoped mental
   model.
4. Join distributed tables only to (a) colocated distributed tables on `group_id`,
   or (b) reference tables. Any other join is a repartition — if you need one,
   it goes in a scheduled worker job with a comment saying so.
5. Prefer `ANY($1::type[])` over `IN (...)` built by string concatenation, and
   never build SQL by interpolation.
6. Verify, don't assume: `EXPLAIN` a new hot query and check `Task Count: 1`.
   `qa/integration/test_citus_topology.py` asserts this for the queries that matter.

**Migrations** are raw SQL in `op.execute` — shard keys and colocation must be
visible in the diff, not inferred from a model. Both `upgrade()` and `downgrade()`
must work; CI runs upgrade → downgrade → upgrade.

## 5. Libraries

Prefer the Rust/C-backed option when there is a real one; the list of what we
actually use and why is in `docs/site/content/docs/architecture.mdx` §2. Do not add a dependency
that duplicates one already present.

**LLM work:** never call a provider SDK from a handler. Go through
`cb_core.llm.router()` with a *task* name (`chat`, `moderate`, `summarize`,
`vision`, `transcribe`). Model choice is configuration. Sampling parameters,
thinking config and effort are filtered per model by `cb_core/llm/catalog.py` —
current Claude models 400 on `temperature`, so passing it through blindly breaks
the default model.

**Storage:** never touch a cloud SDK directly. Go through `cb_core.storage.media()`
for anything user-supplied (it dedupes by content hash and records the reference
row) or `cb_core.storage.store()` for raw blobs.

**Cython:** only pure-CPU, no-IO modules, only with a measured ≥1.5× win
(`python scripts/cb.py bench-baseline && … cython && … bench`). Plain type annotations are
not enough — extension types (`@cython.cclass`) are what pays.

## 6. Testing — the pyramid

Bottom to top, widest to narrowest:

| Layer | Location | Needs infra | What belongs here |
|---|---|---|---|
| **Unit** | `packages/*/tests/` | no | pure functions, parsers, cooldown maths, catalog gating, key derivation. Fast, hundreds of them. |
| **Integration** | `qa/integration/` | Postgres/Citus | repositories and services against a real database, with real users and groups seeded. Also Citus topology assertions. |
| **Acceptance (BDD)** | `qa/features/` + `qa/test_*.py` | mock Telegram, sometimes DB | one scenario per QA feature file, in Gherkin, driving the real handler stack. |

Rules:

- Every bug fix starts with a failing unit test at the lowest layer that can express it.
- Integration tests **simulate users**: create a group, create members, have them
  send messages, assert the resulting rows. Use `qa/integration/factories.py`; do
  not hand-write INSERTs in a test.
- Integration tests skip cleanly when no database is reachable
  (`pytest.mark.integration`), so `python scripts/cb.py test` works offline and CI runs the full set.
- Gherkin lives in `qa/features/` and is kept in sync with `../Cookiebot-QA`. When
  they diverge, fix the divergence — do not fork the spec.
- No mocking of our own code in acceptance tests. Mock the outside world only
  (Telegram, LLM providers, object storage).

## 7. Style

- `ruff` decides formatting and lint; `python scripts/cb.py fmt` before committing. No debates.
- **Everything is annotated, and the gate enforces it.** Ruff runs `ANN` (every
  argument, return, `*args`/`**kwargs`) and mypy runs with `disallow_untyped_defs`,
  `disallow_incomplete_defs` and `check_untyped_defs`. `python scripts/cb.py types`
  runs both, and `cb.py check` runs it.
- Annotations must be **true**, not decorative. `ANN401` is off because `Any` is
  correct at some seams (asyncpg `Record`, aiogram payloads, monkeypatch points);
  an `Any` that hides a type you know is a defect, not a shortcut.
- **In the Cython-compiled modules an annotation is a C type.** `setup.py` compiles
  `HOT_MODULES` with `annotation_typing = True`, so a hint there is lowered to a C
  type and a missing one leaves a `PyObject*` whose every operation goes back
  through the interpreter. `scripts/hot_types.py --check` audits those files down
  to their locals; it is the only place local annotations are mandatory, because
  it is the only place they change what runs. An exemption needs
  `# hot-types: ignore <reason>` and the reason is printed in the report.
  Everywhere else, annotate for the reader and the type checker — it has no
  runtime effect.
- `from __future__ import annotations` everywhere.
- Comments explain **why**, never what. A comment that restates the next line is noise.
- Docstrings on modules and public classes; reference the v1 file:line being
  replaced when the code exists because of a v1 defect.
- Log with `structlog` and event-style names (`media.gc`, `llm.refused`), never
  f-strings. Errors carry `error=str(exc)`, never a formatted message.
- Never label a Prometheus metric with `group_id` or `user_id` — that is a
  cardinality bomb. Per-group numbers come from Citus.

## 8. What not to do

- Do not add a `# type: ignore` or `# noqa` without a reason on the same line.
- Do not widen an exception handler to make a test pass.
- Do not introduce a second way to do something that already has one (a second
  HTTP client, a second settings mechanism, a second migration path).
- Do not change generated files by hand (`*.c`, `*.so`, lockfiles).
- Do not commit a feature whose Gherkin scenario is still red.
