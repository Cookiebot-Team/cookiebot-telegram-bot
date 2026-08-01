import Link from 'next/link';
import { percent, progress, statusBar } from '@/lib/progress';
import { StatusBar } from '@/components/progress';

/** The landing page leads with the product, not the rebuild.
 *
 * Most people who arrive here run a Telegram group and want the bot to stop a
 * sticker flood — the migration board matters to a handful of contributors.
 * The progress panel stays (this is built in the open, and hiding it would be
 * a different kind of dishonesty), but it sits below the thing a group owner
 * came for.
 */
const userLinks = [
  {
    href: '/docs/using',
    title: 'Getting started',
    body: 'Add the bot, promote it, set a language, a welcome and the rules.',
  },
  {
    href: '/docs/using/commands',
    title: 'Commands',
    body: 'Every command, with its Portuguese and Spanish spellings.',
  },
  {
    href: '/docs/using/configure',
    title: 'Configuring a group',
    body: 'Every setting in the /config menu — and the two most groups get wrong.',
  },
  {
    href: '/docs/using/moderation',
    title: 'Moderation',
    body: 'The captcha, sticker floods, the media hold, blocked accounts.',
  },
];

export default function HomePage() {
  const t = progress.totals;

  return (
    <main className="flex flex-1 flex-col">
      <section className="mx-auto w-full max-w-5xl px-6 pt-20 pb-4">
        <p className="text-sm font-medium text-fd-muted-foreground">
          Cookiebot · Telegram group bot
        </p>
        <h1 className="mt-3 text-4xl font-bold tracking-tight text-balance sm:text-5xl">
          Keeps a Telegram group habitable.
        </h1>
        <p className="mt-4 max-w-2xl text-lg text-fd-muted-foreground text-pretty">
          Greets new members, screens joiners, holds the rules, and keeps sticker floods and
          drive-by spam out — in English, Portuguese and Spanish. Telegram is the whole interface:
          no account, no dashboard.
        </p>

        <div className="mt-7 flex flex-wrap gap-3">
          <Link
            href="/docs/using"
            className="rounded-lg bg-fd-primary px-4 py-2 text-sm font-medium text-fd-primary-foreground"
          >
            Set it up in five minutes
          </Link>
          <Link
            href="/docs/using/commands"
            className="rounded-lg border px-4 py-2 text-sm font-medium"
          >
            See the commands
          </Link>
        </div>
      </section>

      <section className="mx-auto grid w-full max-w-5xl gap-4 px-6 py-10 sm:grid-cols-2">
        {userLinks.map((link) => (
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

      <section className="mx-auto w-full max-w-5xl px-6 pt-6 pb-24">
        <h2 className="text-xl font-semibold">Being rebuilt in the open</h2>
        <p className="mt-2 max-w-2xl text-fd-muted-foreground text-pretty">
          v2 is a rewrite that has to earn every switch-over: a feature moves only once its
          behaviour is captured as an executable scenario and that scenario passes. Groups keep
          running on the current release until then. Every number below is measured — the spec, the
          ported scenarios and a real test run — not typed in by hand.
        </p>

        <div className="mt-6 rounded-2xl border bg-fd-card p-6">
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

        <div className="mt-4 flex flex-wrap gap-3">
          <Link href="/docs/progress" className="rounded-lg border px-4 py-2 text-sm font-medium">
            Progress board
          </Link>
          <Link
            href="/docs/architecture"
            className="rounded-lg border px-4 py-2 text-sm font-medium"
          >
            Architecture
          </Link>
        </div>
      </section>
    </main>
  );
}
