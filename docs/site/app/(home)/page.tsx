import Link from 'next/link';
import { percent, progress, statusBar } from '@/lib/progress';
import { StatusBar } from '@/components/progress';

const links = [
  {
    href: '/docs/progress',
    title: 'Progress',
    body: 'Where the v1 → v2 port stands: features, milestones, scenarios, carried defects.',
  },
  {
    href: '/docs/features',
    title: 'Features',
    body: 'One page per feature — what it does, what it must keep doing, and whether it does it yet.',
  },
  {
    href: '/docs/architecture',
    title: 'Architecture',
    body: 'How v2 is built: four services, Citus, the compiled hot path, and why each choice was made.',
  },
  {
    href: '/docs/development',
    title: 'Development',
    body: 'Setup, tasks, the test pyramid, and the gates a change has to clear.',
  },
];

export default function HomePage() {
  const t = progress.totals;

  return (
    <main className="flex flex-1 flex-col">
      <section className="mx-auto w-full max-w-5xl px-6 pt-20 pb-10">
        <p className="text-sm font-medium text-fd-muted-foreground">
          Cookiebot · Telegram group bot
        </p>
        <h1 className="mt-3 text-4xl font-bold tracking-tight text-balance sm:text-5xl">
          A v1 bot, rebuilt in the open — one feature, one scenario at a time.
        </h1>
        <p className="mt-4 max-w-2xl text-lg text-fd-muted-foreground text-pretty">
          Every number on this site is measured, not claimed: the spec, the ported Gherkin
          scenarios and a real test run, rendered together. A feature marked done with no passing
          scenario shows up as a finding, not a footnote.
        </p>

        <div className="mt-8 rounded-2xl border bg-fd-card p-6">
          <div className="flex flex-wrap items-baseline justify-between gap-3">
            <div>
              <div className="text-sm font-medium">Features ported</div>
              <div className="mt-1 text-4xl font-semibold tabular-nums">
                {t.done}
                <span className="text-fd-muted-foreground">/{t.features}</span>
                <span className="ml-2 text-base font-normal text-fd-muted-foreground">
                  {percent(t.done, t.features)}%
                </span>
              </div>
            </div>
            <dl className="flex gap-6 text-sm">
              <div>
                <dt className="text-fd-muted-foreground">v1 specs covered</dt>
                <dd className="text-lg font-semibold tabular-nums">
                  {t.spec_files_covered}/{t.spec_files}
                </dd>
              </div>
              <div>
                <dt className="text-fd-muted-foreground">Scenarios green</dt>
                <dd className="text-lg font-semibold tabular-nums">{t.green}</dd>
              </div>
              <div>
                <dt className="text-fd-muted-foreground">Failing</dt>
                <dd
                  className={`text-lg font-semibold tabular-nums ${t.failing ? 'text-red-600 dark:text-red-400' : ''}`}
                >
                  {t.failing}
                </dd>
              </div>
            </dl>
          </div>

          <StatusBar
            className="mt-5"
            done={t.done}
            partial={t.partial}
            blocked={t.blocked}
            total={t.features}
          />

          <div className="mt-4 flex flex-wrap gap-x-5 gap-y-1 text-xs text-fd-muted-foreground">
            <span className="inline-flex items-center gap-1.5">
              <span className={`size-2 rounded-full ${statusBar.done}`} /> done {t.done}
            </span>
            <span className="inline-flex items-center gap-1.5">
              <span className={`size-2 rounded-full ${statusBar.partial}`} /> partial {t.partial}
            </span>
            <span className="inline-flex items-center gap-1.5">
              <span className={`size-2 rounded-full ${statusBar.planned}`} /> planned {t.planned}
            </span>
            <span className="ml-auto">
              generated {progress.generated_at} · <code>{progress.commit}</code>
            </span>
          </div>
        </div>

        <div className="mt-6 flex flex-wrap gap-3">
          <Link
            href="/docs/progress"
            className="rounded-lg bg-fd-primary px-4 py-2 text-sm font-medium text-fd-primary-foreground"
          >
            See the progress board
          </Link>
          <Link href="/docs" className="rounded-lg border px-4 py-2 text-sm font-medium">
            Read the docs
          </Link>
        </div>
      </section>

      <section className="mx-auto grid w-full max-w-5xl gap-4 px-6 pb-24 sm:grid-cols-2">
        {links.map((link) => (
          <Link
            key={link.href}
            href={link.href}
            className="rounded-xl border bg-fd-card p-5 transition-colors hover:bg-fd-accent"
          >
            <div className="font-medium">{link.title}</div>
            <p className="mt-1 text-sm text-fd-muted-foreground">{link.body}</p>
          </Link>
        ))}
      </section>
    </main>
  );
}
