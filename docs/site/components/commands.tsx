/** The command reference, rendered from the bot's own alias table.
 *
 * `content/progress.json` carries every command `cb_core.textmatch` accepts,
 * because a hand-kept command list is the table on this site a reader is most
 * likely to *act* on and the one that goes stale first — an alias added in the
 * parser and forgotten here becomes a command a group is told does not exist.
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
  const selected = only
    ? only.map((name) => rows.find((row) => row.primary === name)).filter(Boolean as never as (r: CommandRow | undefined) => r is CommandRow)
    : rows.filter((row) => !area || row.area === area);

  if (selected.length === 0)
    return (
      <p className="text-sm text-fd-muted-foreground">
        No commands here yet — run <code>python scripts/cb.py sandbox-config</code> then{' '}
        <code>docs-sync</code>.
      </p>
    );

  return (
    <div className="not-prose my-6 overflow-x-auto rounded-xl border">
      <table className="w-full min-w-[40rem] border-collapse text-sm">
        <thead className="bg-fd-muted/50 text-left">
          <tr>
            <th className="px-4 py-2.5 font-medium">Command</th>
            <th className="px-4 py-2.5 font-medium">Also written</th>
            <th className="px-4 py-2.5 font-medium">What it does</th>
            <th className="px-4 py-2.5 font-medium">In v2</th>
          </tr>
        </thead>
        <tbody>
          {selected.map((row) => (
            <tr key={row.primary} className="border-t align-top hover:bg-fd-muted/30">
              <td className="px-4 py-2.5">
                <code className="whitespace-nowrap">{row.primary}</code>
              </td>
              <td className="px-4 py-2.5">
                <div className="flex flex-wrap gap-1">
                  {row.aliases.length === 0 ? (
                    <span className="text-xs text-fd-muted-foreground">—</span>
                  ) : (
                    row.aliases.map((alias) => (
                      <code key={alias} className="text-xs whitespace-nowrap opacity-80">
                        {alias}
                      </code>
                    ))
                  )}
                </div>
              </td>
              <td className="px-4 py-2.5">
                {describe?.[row.primary] ?? row.title}
                {row.url ? (
                  <>
                    {' '}
                    <Link href={row.url} className="text-xs text-fd-muted-foreground underline">
                      details
                    </Link>
                  </>
                ) : null}
              </td>
              <td className="px-4 py-2.5">
                <Availability status={row.status} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
