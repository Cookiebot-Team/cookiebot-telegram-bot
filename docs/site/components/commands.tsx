/** The command reference, rendered from the bot's own alias table.
 *
 * `content/progress.json` carries every command `cb_core.textmatch` accepts,
 * because a hand-kept command list is the table on this site a reader is most
 * likely to *act* on and the one that goes stale first — an alias added in the
 * parser and forgotten here becomes a command a group is told does not exist.
 *
 * Two renderings of the same rows: `<CommandTable>` for the reference page,
 * where a reader is scanning for one name, and `<CommandCards>` for the guide
 * pages, where each command needs a sentence and its own breathing room.
 */
import Link from 'next/link';
import { type Status, progress, statusBar } from '@/lib/progress';

interface CommandRow {
  primary: string;
  aliases: string[];
  feature_id: string | null;
  area: string;
  title: string;
  hint: string;
  status: string;
  url: string | null;
}

const rows = (progress as unknown as { commands?: CommandRow[] }).commands ?? [];

/** What a group owner actually needs to know about a command's readiness, in
 * their words rather than the migration spec's. `partial` is deliberately not
 * softened: half a feature is the case where a person needs to know. */
const availability: Record<string, { label: string; status: Status }> = {
  done: { label: 'Live', status: 'done' },
  partial: { label: 'Partly live', status: 'partial' },
  planned: { label: 'Coming', status: 'planned' },
  blocked: { label: 'Blocked', status: 'blocked' },
  unknown: { label: 'Coming', status: 'planned' },
};

function Availability({ status }: { status: string }) {
  const { label, status: mapped } = availability[status] ?? availability.unknown;
  return (
    <span className="inline-flex items-center gap-1.5 whitespace-nowrap text-xs text-fd-muted-foreground">
      <span className={`size-1.5 rounded-full ${statusBar[mapped]}`} />
      {label}
    </span>
  );
}

function select(only?: string[], area?: string): CommandRow[] {
  if (only)
    return only
      .map((name) => rows.find((row) => row.primary === name))
      .filter((row): row is CommandRow => row !== undefined);
  return rows.filter((row) => !area || row.area === area);
}

function Empty() {
  return (
    <p className="text-sm text-fd-muted-foreground">
      No commands here yet — run <code>python scripts/cb.py sandbox-config</code> then{' '}
      <code>docs-sync</code>.
    </p>
  );
}

/** One table per group of commands. `only` names them explicitly (the order is
 * the order given); `area` takes everything in a feature area. */
export function CommandTable({
  only,
  area,
  describe,
}: {
  only?: string[];
  area?: string;
  describe?: Record<string, string>;
}) {
  const selected = select(only, area);
  if (selected.length === 0) return <Empty />;

  return (
    <div className="cb-stack-table not-prose my-6 overflow-x-auto rounded-cb border border-fd-border bg-fd-card">
      <table className="w-full min-w-[40rem] border-collapse text-sm">
        <thead className="bg-fd-muted/60 text-left">
          <tr>
            <th className="px-4 py-2.5 font-medium">Command</th>
            <th className="px-4 py-2.5 font-medium">Also written</th>
            <th className="px-4 py-2.5 font-medium">What it does</th>
            <th className="px-4 py-2.5 font-medium">In v2</th>
          </tr>
        </thead>
        <tbody>
          {selected.map((row) => (
            <tr
              key={row.primary}
              className="border-t border-fd-border align-top hover:bg-cb-gold-500/6"
            >
              <td className="px-4 py-2.5" data-label="Command">
                <code className="font-mono whitespace-nowrap text-fd-primary">{row.primary}</code>
              </td>
              <td
                className="px-4 py-2.5"
                data-label="Also written"
                data-empty={row.aliases.length === 0}
              >
                <div className="flex flex-wrap gap-1">
                  {row.aliases.length === 0 ? (
                    <span className="text-xs text-fd-muted-foreground">—</span>
                  ) : (
                    row.aliases.map((alias) => (
                      <code
                        key={alias}
                        className="font-mono text-xs whitespace-nowrap text-fd-muted-foreground"
                      >
                        {alias}
                      </code>
                    ))
                  )}
                </div>
              </td>
              <td className="px-4 py-2.5" data-label="What it does">
                <span>
                  {describe?.[row.primary] ?? row.title}
                  {row.url ? (
                    <>
                      {' '}
                      <Link
                        href={row.url}
                        className="cb-tap text-xs text-fd-muted-foreground underline"
                      >
                        details
                      </Link>
                    </>
                  ) : null}
                </span>
              </td>
              <td className="px-4 py-2.5" data-label="In v2">
                <Availability status={row.status} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** The same rows as cards: the shape a guide page wants, where a command is
 * being explained rather than looked up. `describe` carries the sentence,
 * `note` the one caveat that would otherwise become a Callout per command. */
export function CommandCards({
  only,
  area,
  describe,
  note,
}: {
  only?: string[];
  area?: string;
  describe?: Record<string, string>;
  note?: Record<string, string>;
}) {
  const selected = select(only, area);
  if (selected.length === 0) return <Empty />;

  return (
    <div className="not-prose my-6 grid gap-3 sm:grid-cols-2">
      {selected.map((row) => (
        <div
          key={row.primary}
          className="rounded-cb border border-fd-border bg-fd-card p-4 transition-colors hover:border-cb-gold-500/50"
        >
          <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
            <code className="font-mono text-sm font-bold text-fd-primary">{row.primary}</code>
            {row.aliases.map((alias) => (
              <code key={alias} className="font-mono text-xs text-fd-muted-foreground">
                {alias}
              </code>
            ))}
          </div>
          <p className="mt-2 text-sm text-fd-foreground/90">{describe?.[row.primary] ?? row.title}</p>
          {note?.[row.primary] ? (
            <p className="mt-2 border-l-2 border-cb-blush-400/70 pl-2.5 text-xs text-fd-muted-foreground">
              {note[row.primary]}
            </p>
          ) : null}
        </div>
      ))}
    </div>
  );
}
