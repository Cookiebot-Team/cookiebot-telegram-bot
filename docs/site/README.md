# docs/site — the documentation and progress site

[Fumadocs](https://fumadocs.dev) on Next.js, exported statically and published
to GitHub Pages by `.github/workflows/docs.yml`.

```bash
python scripts/cb.py docs          # dev server on :3002
python scripts/cb.py docs-build    # types:check + the static export CI publishes
python scripts/cb.py docs-sync     # regenerate the measured half (runs the suite)
```

bun, not npm — `bun.lock` is the committed lockfile.

## Two kinds of page, and the line between them

**Written.** `content/docs/*.mdx` — the handbook (architecture, development,
sandbox, deploy, …), the section intros, and the prose in every feature page.
Humans own these. Nothing generated ever overwrites them.

**Measured.** `content/progress.json` and the **frontmatter** of every
`content/docs/features/*.mdx`, written by `scripts/docs_sync.py` from
`scripts/spec.py` (the migration spec) plus `scripts/status.py` (which counts
Gherkin scenarios and runs the offline suite).

That split is the whole design. A page can say whatever it likes about *how* a
feature works; it cannot claim the feature is done — that comes from the spec,
and `cb.py docs-sync --check` (part of `cb.py check`) fails when a page's
frontmatter disagrees with it.

```
scripts/spec.py ──┐
                  ├─► scripts/docs_sync.py ─┬─► content/progress.json ─► lib/progress.ts ─► components/progress.tsx
scripts/status.py ┘                         └─► features/*.mdx frontmatter
```

## Adding a feature page

You don't. Add the row to `scripts/spec.py` and run `cb.py docs-sync`: the page
is created with a stub body, listed in the sidebar, and included in every
table. Then write the three sections it asks you for.

## Components available in MDX

Registered globally in `components/mdx.tsx`, so no imports in a page:

| Component | Renders |
|---|---|
| `<ProgressOverview />` | The headline block: features ported, specs covered, green, failing |
| `<MilestoneProgress />` | One bar per milestone |
| `<FeatureTable area? milestone? status? />` | The feature ledger, optionally filtered |
| `<FeatureHeader id="core_rules" />` | A feature page's generated header |
| `<ScenarioLedger />` | Every spec file: written, ported, green, failing |
| `<DefectTable />` | The carried v1 defects |
| `<ConsistencyFindings />` | Where the spec and the suite disagree |
| `<StatusBadge status="done" />` / `<StatusBar … />` | The primitives |

None of them takes a number as a prop. A component that could be handed a
hand-typed percentage is a component that will eventually show a stale one.

## MDX traps worth knowing

- HTML comments break the parser — use `{/* … */}`.
- `<https://example.com>` autolinks parse as JSX. Write a normal link.
- A bare `{` in prose starts an expression. Wrap it in backticks.

## Layout

```
app/(home)/page.tsx      the landing page (hero + live progress snapshot)
app/docs/                the documentation shell and page route
components/progress.tsx  every progress rendering on the site
components/mdx.tsx       global MDX component registry
lib/source.ts            content source + the feature frontmatter schema
lib/progress.ts          typed access to content/progress.json
content/docs/            the pages
content/progress.json    generated — do not edit
```
