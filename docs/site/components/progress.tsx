/** Every rendering of "how far along is this" on the site.
 *
 * All of it reads `lib/progress.ts`, which reads one generated file. There is
 * deliberately no component here that takes a hand-typed number as a prop: a
 * page that can state a percentage of its own is a page that will state a
 * stale one, and the reason this site exists is that a person should be able
 * to trust the first screen they land on.
 */
import Link from 'next/link';
import {
  type DefectRow,
  type FeatureRow,
  type Status,
  percent,
  progress,
  statusBar,
  statusClass,
  statusLabel,
} from '@/lib/progress';
import { repoFile } from '@/lib/shared';

export function StatusBadge({ status, className = '' }: { status: Status; className?: string }) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset whitespace-nowrap ${statusClass[status]} ${className}`}
    >
      <span className={`size-1.5 rounded-full ${statusBar[status]}`} />
      {statusLabel[status]}
    </span>
  );
}

/** A stacked bar: done / partial / planned+blocked, in the site's one colour
 * vocabulary. Stacked rather than a single percentage because "40% done" and
 * "40% done with a third in flight" are different project states and the
 * single number hides which one you are looking at. */
export function StatusBar({
  done,
  partial = 0,
  blocked = 0,
  total,
  className = '',
}: {
  done: number;
  partial?: number;
  blocked?: number;
  total: number;
  className?: string;
}) {
  const width = (n: number) => `${total === 0 ? 0 : (n / total) * 100}%`;
  return (
    <div className={`flex h-2 w-full overflow-hidden rounded-full bg-fd-muted ${className}`}>
      <div className={statusBar.done} style={{ width: width(done) }} />
      <div className={statusBar.partial} style={{ width: width(partial) }} />
      <div className={statusBar.blocked} style={{ width: width(blocked) }} />
    </div>
  );
}

function Stat({
  label,
  value,
  sub,
  accent = '',
}: {
  label: string;
  value: string | number;
  sub?: string;
  accent?: string;
}) {
  return (
    <div className="rounded-xl border bg-fd-card p-4">
      <div className="text-xs font-medium tracking-wide text-fd-muted-foreground uppercase">
        {label}
      </div>
      <div className={`mt-1 text-3xl font-semibold tabular-nums ${accent}`}>{value}</div>
      {sub ? <div className="mt-1 text-xs text-fd-muted-foreground">{sub}</div> : null}
    </div>
  );
}

/** The first screen: where the port stands, in four numbers and one bar. */
export function ProgressOverview() {
  const t = progress.totals;
  return (
    <div className="not-prose my-6 space-y-5">
      <div className="rounded-xl border bg-fd-card p-5">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <div className="text-sm font-medium">Features ported</div>
          <div className="text-sm text-fd-muted-foreground tabular-nums">
            {t.done}/{t.features} · {percent(t.done, t.features)}%
          </div>
        </div>
        <StatusBar
          className="mt-3"
          done={t.done}
          partial={t.partial}
          blocked={t.blocked}
          total={t.features}
        />
        <div className="mt-3 flex flex-wrap gap-x-5 gap-y-1 text-xs text-fd-muted-foreground">
          <Legend status="done" count={t.done} />
          <Legend status="partial" count={t.partial} />
          <Legend status="planned" count={t.planned} />
          <Legend status="blocked" count={t.blocked} />
        </div>
      </div>

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Stat
          label="v1 specs covered"
          value={`${t.spec_files_covered}/${t.spec_files}`}
          sub={`spec files with an executable scenario · ${t.spec_scenarios} v1 scenarios written`}
        />
        <Stat
          label="Scenarios green"
          value={t.green}
          sub={`executed and passing · ${t.ported} ported from v1 specs`}
        />
        <Stat
          label="Scenarios failing"
          value={t.failing}
          sub={t.failing === 0 ? 'nothing red' : 'needs attention'}
          accent={t.failing > 0 ? 'text-cb-error-ink dark:text-cb-error' : ''}
        />
        <Stat label="New v2 specs" value={t.new_specs} sub="no v1 equivalent" />
      </div>

      <p className="text-xs text-fd-muted-foreground">
        Offline suite at generation time: <span className="font-medium">{progress.test_summary}</span>.
        Measured from commit <code>{progress.commit}</code> on {progress.generated_at}.
      </p>
    </div>
  );
}

function Legend({ status, count }: { status: Status; count: number }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className={`size-2 rounded-full ${statusBar[status]}`} />
      {statusLabel[status]} <span className="tabular-nums">{count}</span>
    </span>
  );
}

/** Milestones as rows, because the useful question is never "how much is left"
 * but "what is left before the next thing ships". */
export function MilestoneProgress() {
  return (
    <div className="not-prose my-6 space-y-3">
      {progress.milestones.map((milestone) => (
        <div key={milestone.id} className="rounded-xl border bg-fd-card p-4">
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <div className="text-sm font-medium">
              <span className="font-mono text-fd-muted-foreground">{milestone.id}</span>{' '}
              {milestone.title}
            </div>
            <div className="text-sm tabular-nums text-fd-muted-foreground">
              {milestone.done}/{milestone.total}
            </div>
          </div>
          <StatusBar className="mt-3" done={milestone.done} total={milestone.total} />
        </div>
      ))}
    </div>
  );
}

function scenarioCell(feature: FeatureRow) {
  const { spec, ported, green, failing } = feature.scenarios;
  if (failing > 0)
    return <span className="text-cb-error-ink dark:text-cb-error">{failing} failing</span>;
  if (green > 0) return <span className="tabular-nums">{green} green</span>;
  if (ported > 0) return <span className="tabular-nums">{ported} ported</span>;
  if (spec > 0) return <span className="text-fd-muted-foreground">{spec} to port</span>;
  return <span className="text-fd-muted-foreground">—</span>;
}

/** The whole ledger, or one slice of it. `area`/`milestone`/`status` filter;
 * they are the three axes anybody actually asks about. */
export function FeatureTable({
  area,
  milestone,
  status,
}: {
  area?: string;
  milestone?: string;
  status?: Status;
}) {
  const rows = progress.features.filter(
    (feature) =>
      (!area || feature.area === area) &&
      (!milestone || feature.milestone === milestone) &&
      (!status || feature.status === status),
  );

  if (rows.length === 0)
    return <p className="text-sm text-fd-muted-foreground">Nothing matches that filter.</p>;

  return (
    <div className="cb-stack-table not-prose my-6 overflow-x-auto rounded-xl border">
      <table className="w-full min-w-[46rem] border-collapse text-sm">
        <thead className="bg-fd-muted/50 text-left">
          <tr>
            <th className="px-4 py-2.5 font-medium">Feature</th>
            <th className="px-4 py-2.5 font-medium">Status</th>
            <th className="px-4 py-2.5 font-medium">Milestone</th>
            <th className="px-4 py-2.5 font-medium">Layer</th>
            <th className="px-4 py-2.5 font-medium">Scenarios</th>
            <th className="px-4 py-2.5 font-medium">Triggers</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((feature) => (
            <tr key={feature.id} className="border-t align-top hover:bg-fd-muted/30">
              <td className="px-4 py-2.5" data-label="Feature">
                <Link
                  href={feature.url}
                  className="cb-tap font-medium text-fd-foreground hover:underline"
                >
                  {feature.title}
                </Link>
                <div className="font-mono text-xs text-fd-muted-foreground">{feature.id}</div>
              </td>
              <td className="px-4 py-2.5" data-label="Status">
                <StatusBadge status={feature.status} />
              </td>
              <td className="px-4 py-2.5 font-mono text-xs" data-label="Milestone">
                {feature.milestone}
              </td>
              <td className="px-4 py-2.5 text-xs text-fd-muted-foreground" data-label="Layer">
                {feature.layer}
              </td>
              <td className="px-4 py-2.5 text-xs" data-label="Scenarios">
                {scenarioCell(feature)}
              </td>
              <td
                className="px-4 py-2.5"
                data-label="Triggers"
                data-empty={feature.triggers.length === 0}
              >
                <div className="flex flex-wrap gap-1">
                  {feature.triggers.length === 0 ? (
                    <span className="text-xs text-fd-muted-foreground">—</span>
                  ) : (
                    feature.triggers.map((trigger) => (
                      <code
                        key={trigger}
                        className="rounded bg-fd-muted px-1.5 py-0.5 text-xs whitespace-nowrap"
                      >
                        {trigger}
                      </code>
                    ))
                  )}
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** The header of a feature page: everything the spec knows, before a word of
 * hand-written prose. Reads the row by id rather than taking props, so a page
 * cannot describe itself as done while the spec says planned. */
export function FeatureHeader({ id }: { id: string }) {
  const feature = progress.features.find((row) => row.id === id);
  if (!feature)
    return (
      <p className="text-sm text-cb-error-ink dark:text-cb-error">
        No row for <code>{id}</code> in <code>scripts/spec.py</code> — run{' '}
        <code>python scripts/cb.py docs-sync</code>.
      </p>
    );

  const { spec, ported, green, failing } = feature.scenarios;
  return (
    <div className="not-prose my-6 space-y-4 rounded-xl border bg-fd-card p-5">
      <div className="flex flex-wrap items-center gap-2">
        <StatusBadge status={feature.status} />
        <span className="rounded-full bg-fd-muted px-2 py-0.5 font-mono text-xs">
          {feature.milestone}
        </span>
        <span className="rounded-full bg-fd-muted px-2 py-0.5 text-xs">{feature.layer}</span>
        <span className="rounded-full bg-fd-muted px-2 py-0.5 text-xs">{feature.area}</span>
      </div>

      {feature.triggers.length > 0 ? (
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-xs text-fd-muted-foreground">Triggers</span>
          {feature.triggers.map((trigger) => (
            <code key={trigger} className="rounded bg-fd-muted px-1.5 py-0.5 text-xs">
              {trigger}
            </code>
          ))}
        </div>
      ) : null}

      <dl className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
        <Pair label="v1 scenarios" value={spec || '—'} />
        <Pair label="Ported" value={ported || '—'} />
        <Pair label="Green" value={green || '—'} />
        <Pair
          label="Failing"
          value={failing || '—'}
          accent={failing > 0 ? 'text-cb-error-ink dark:text-cb-error' : ''}
        />
      </dl>

      {feature.notes ? <p className="text-sm text-fd-muted-foreground">{feature.notes}</p> : null}

      <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs">
        {feature.contract ? (
          <a className="underline" href={repoFile(feature.contract)}>
            behaviour contract
          </a>
        ) : null}
        {feature.v1_source ? (
          <span className="text-fd-muted-foreground">
            v1 source: <code>{feature.v1_source}</code>
          </span>
        ) : null}
      </div>
    </div>
  );
}

function Pair({
  label,
  value,
  accent = '',
}: {
  label: string;
  value: string | number;
  accent?: string;
}) {
  return (
    <div>
      <dt className="text-xs text-fd-muted-foreground">{label}</dt>
      <dd className={`font-medium tabular-nums ${accent}`}>{value}</dd>
    </div>
  );
}

export function ScenarioLedger() {
  return (
    <div className="cb-stack-table not-prose my-6 overflow-x-auto rounded-xl border">
      <table className="w-full min-w-[38rem] border-collapse text-sm">
        <thead className="bg-fd-muted/50 text-left">
          <tr>
            <th className="px-4 py-2.5 font-medium">Spec</th>
            <th className="px-4 py-2.5 font-medium text-right">v1</th>
            <th className="px-4 py-2.5 font-medium text-right">Ported</th>
            <th className="px-4 py-2.5 font-medium text-right">Green</th>
            <th className="px-4 py-2.5 font-medium text-right">Failing</th>
            <th className="px-4 py-2.5 font-medium">State</th>
          </tr>
        </thead>
        <tbody>
          {progress.scenarios.map((row) => (
            <tr key={row.stem} className="border-t hover:bg-fd-muted/30">
              <td className="px-4 py-2 font-mono text-xs" data-label="Spec">
                {row.stem}
              </td>
              <td className="px-4 py-2 text-right tabular-nums" data-label="v1 scenarios">
                {row.spec || '—'}
              </td>
              <td className="px-4 py-2 text-right tabular-nums" data-label="Ported">
                {row.ported || '—'}
              </td>
              <td className="px-4 py-2 text-right tabular-nums" data-label="Green">
                {row.green || '—'}
              </td>
              <td
                className={`px-4 py-2 text-right tabular-nums ${row.failing ? 'text-cb-error-ink dark:text-cb-error' : ''}`}
                data-label="Failing"
              >
                {row.failing || '—'}
              </td>
              <td className="px-4 py-2 text-xs" data-label="State">
                {row.state}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function DefectTable() {
  return (
    <div className="cb-stack-table not-prose my-6 overflow-x-auto rounded-xl border">
      <table className="w-full min-w-[36rem] border-collapse text-sm">
        <thead className="bg-fd-muted/50 text-left">
          <tr>
            <th className="px-4 py-2.5 font-medium">#</th>
            <th className="px-4 py-2.5 font-medium">Defect carried from v1</th>
            <th className="px-4 py-2.5 font-medium">Addressed by design</th>
          </tr>
        </thead>
        <tbody>
          {progress.defects.map((defect: DefectRow) => (
            <tr key={defect.id} className="border-t align-top hover:bg-fd-muted/30">
              <td className="px-4 py-2 font-mono text-xs" data-label="Defect">
                {defect.id}
              </td>
              <td className="px-4 py-2" data-label="Carried from v1">
                <span>{defect.text}</span>
              </td>
              <td className="px-4 py-2 text-xs" data-label="Addressed">
                {defect.addressed ? (
                  <span className="text-cb-success-ink dark:text-cb-success">yes</span>
                ) : (
                  <span className="text-cb-warning-ink dark:text-cb-warning">not yet</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** What the spec and reality disagree about, if anything. Rendered loudly and
 * never suppressed: `cb.py status --check` already fails CI on these, and a
 * dashboard that quietly omits them is how a project convinces itself it is
 * further along than it is. */
export function ConsistencyFindings() {
  if (progress.problems.length === 0)
    return (
      <div className="not-prose my-6 rounded-xl border border-cb-success/30 bg-cb-success/8 p-4 text-sm">
        No inconsistencies. Every feature marked <strong>done</strong> has a ported, passing
        scenario, and every QA spec has a row in <code>scripts/spec.py</code>.
      </div>
    );

  return (
    <div className="not-prose my-6 rounded-xl border border-cb-warning/30 bg-cb-warning/8 p-4">
      <div className="text-sm font-medium">
        {progress.problems.length} finding{progress.problems.length === 1 ? '' : 's'}
      </div>
      <ul className="mt-2 list-disc space-y-1 pl-5 text-sm">
        {progress.problems.map((problem) => (
          <li key={problem}>{problem}</li>
        ))}
      </ul>
    </div>
  );
}
