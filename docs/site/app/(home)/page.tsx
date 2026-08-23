import type { CSSProperties } from 'react';
import Link from 'next/link';
import { percent, progress, statusBar } from '@/lib/progress';
import { StatusBar } from '@/components/progress';
import { asset } from '@/lib/shared';

/** The landing page leads with the product, not the rebuild.
 *
 * Most people who arrive here run a Telegram group and want the bot to stop a
 * sticker flood — the migration board matters to a handful of contributors.
 * The progress panel stays (this is built in the open, and hiding it would be
 * a different kind of dishonesty), but it sits below the thing a group owner
 * came for.
 *
 * Every colour here is a token from `app/theme.css`, which takes the bot's own
 * avatar and the web hub's palette as its two sources. No hex codes in this
 * file, and no second brand.
 */
const capabilities = [
  {
    href: '/docs/using/moderation',
    icon: '🛡️',
    title: 'Guards the chat',
    body: 'A captcha at the door with an admin override, three block lists, a per-group sticker-flood limit, and a media hold on brand-new accounts.',
  },
  {
    href: '/docs/using/welcome',
    icon: '👋',
    title: 'Runs the room',
    body: 'Welcome messages, the rules, three languages picked up from whoever added the bot, and a skin per event so a convention can run its own bot.',
  },
  {
    href: '/docs/using/fun',
    icon: '🎲',
    title: 'Is fun to have around',
    body: 'Dice, ships, battles settled by a poll, memes from profile pictures, distorted stickers, fortunes, and old messages dragged back up.',
  },
  {
    href: '/docs/using/utilities',
    icon: '🧰',
    title: 'Does the chores',
    body: 'Birthdays, calling the admins with one confirmed press, YouTube search, and links from X, TikTok and Bluesky rewritten so they preview.',
  },
  {
    href: '/docs/using/ai',
    icon: '🤖',
    title: 'Talks and listens',
    body: 'Answers when you mention it, transcribes voice notes, recognises music, finds where a picture came from — each with its own limits.',
  },
  {
    href: '/docs/using/giveaways',
    icon: '🎁',
    title: "Runs the community's events",
    body: 'Raffles drawn in the group, countdown posters for the partnered conventions, and approved posts carried between partnered groups.',
  },
];

/** A taste of the command surface, in the order a group meets them. Kept
 * short on purpose — the full, generated list is one click away. */
const sampler = [
  '/rules',
  '/config',
  '/newwelcome',
  '/adm',
  '/giveaway',
  '/ship',
  '/meme',
  '/dice',
  '/searchsource',
  '/transcribe',
  '/birthday',
  '/random',
];

/** The count on the "+n more" chip is the parser's, not a number kept in
 * step by hand — the same rule the command tables follow. */
const commandCount = (progress as unknown as { commands?: unknown[] }).commands?.length ?? 0;

export default function HomePage() {
  const t = progress.totals;
  const rest = Math.max(commandCount - sampler.length, 0);

  return (
    <main className="flex flex-1 flex-col">
      {/* Hero */}
      <section className="relative overflow-hidden">
        <div className="cb-glow pointer-events-none absolute inset-0 -z-10" />
        {/* The hub's circuit ornaments, masked so they take a brand colour
            rather than arriving as a foreign black-on-white drawing. */}
        <div
          aria-hidden
          className="cb-wire pointer-events-none absolute top-16 -left-24 -z-10 hidden size-56 text-cb-gold-500/15 md:block"
          style={{ '--cb-wire-src': `url(${asset('/brand/wire-left.svg')})` } as CSSProperties}
        />
        <div
          aria-hidden
          className="cb-wire pointer-events-none absolute -right-24 bottom-4 -z-10 hidden size-56 text-cb-blush-400/15 md:block"
          style={{ '--cb-wire-src': `url(${asset('/brand/wire-right.svg')})` } as CSSProperties}
        />
        <div className="mx-auto grid w-full max-w-5xl gap-8 px-5 pt-12 pb-8 sm:px-6 sm:pt-20 sm:pb-10 md:grid-cols-[1.4fr_1fr] md:gap-10 md:items-center">
          <div>
            <p className="font-mono text-xs tracking-widest text-fd-muted-foreground uppercase">
              Telegram group bot
            </p>
            <h1 className="mt-3 text-4xl font-bold tracking-tight text-balance sm:text-5xl">
              Keeps a Telegram group <span className="cb-gradient-text">habitable</span>.
            </h1>
            <p className="mt-4 max-w-2xl text-lg text-fd-muted-foreground text-pretty">
              Greets new members, screens joiners, holds the rules, and keeps sticker floods and
              drive-by spam out — then stays for the dice, the memes and the giveaways. English,
              Portuguese and Spanish. Telegram is the whole interface: no account, no dashboard.
            </p>

            {/* Stacked and full-bleed on a phone: two side-by-side buttons at
                44px of height each is the shape that gets mis-tapped, and there
                is no second column competing for the width. */}
            <div className="mt-7 flex flex-col gap-3 sm:flex-row sm:flex-wrap">
              <Link
                href="/docs/using"
                className="rounded-cb bg-fd-primary px-4 py-3 text-center text-sm font-semibold text-fd-primary-foreground transition-opacity hover:opacity-90 sm:py-2 sm:text-start"
              >
                Set it up in five minutes
              </Link>
              <Link
                href="/docs/using/commands"
                className="rounded-cb border border-fd-border px-4 py-3 text-center text-sm font-semibold transition-colors hover:bg-fd-accent sm:py-2 sm:text-start"
              >
                See every command
              </Link>
            </div>
          </div>

          <div className="justify-self-center md:justify-self-end">
            <div className="relative">
              <div className="absolute -inset-4 rounded-full bg-cb-gold-500/20 blur-2xl" />
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                src={asset('/brand/cookiebot-avatar.jpg')}
                alt="Cookiebot"
                width={224}
                height={224}
                className="relative size-40 rounded-full ring-2 ring-cb-gold-500/50 sm:size-56"
              />
            </div>
          </div>
        </div>

        {/* The command surface, as a texture rather than a list. */}
        <div className="mx-auto w-full max-w-5xl px-5 pb-10 sm:px-6 sm:pb-12">
          <div className="flex flex-wrap gap-2">
            {sampler.map((command) => (
              <code
                key={command}
                className="rounded-full border border-fd-border bg-fd-card px-3 py-1.5 font-mono text-xs text-fd-muted-foreground sm:py-1"
              >
                {command}
              </code>
            ))}
            <Link
              href="/docs/using/commands"
              className="rounded-full border border-cb-gold-500/40 px-3 py-1.5 font-mono text-xs text-fd-primary sm:py-1"
            >
              +{rest} more
            </Link>
          </div>
        </div>
      </section>

      {/* What it does */}
      <section className="mx-auto w-full max-w-5xl px-5 pb-6 sm:px-6">
        <h2 className="font-display text-2xl tracking-wide">What it does</h2>
        <hr className="cb-rule mt-3" />
        <div className="mt-6 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {capabilities.map((item) => (
            <Link
              key={item.href}
              href={item.href}
              className="group rounded-cb border border-fd-border bg-fd-card p-5 transition-colors hover:border-cb-gold-500/50 hover:bg-fd-accent"
            >
              <div aria-hidden className="text-xl">
                {item.icon}
              </div>
              <div className="mt-2 font-semibold group-hover:text-fd-primary">{item.title}</div>
              <p className="mt-1.5 text-sm text-fd-muted-foreground text-pretty">{item.body}</p>
            </Link>
          ))}
        </div>
      </section>

      {/* Built in the open */}
      <section className="mx-auto w-full max-w-5xl px-5 pt-12 pb-20 sm:px-6 sm:pb-24">
        <h2 className="font-display text-2xl tracking-wide">Being rebuilt in the open</h2>
        <hr className="cb-rule mt-3" />
        <p className="mt-4 max-w-2xl text-fd-muted-foreground text-pretty">
          v2 is a rewrite that has to earn every switch-over: a feature moves only once its
          behaviour is captured as an executable scenario and that scenario passes. Groups keep
          running on the current release until then. Every number below is measured — the spec, the
          ported scenarios and a real test run — not typed in by hand.
        </p>

        <div className="mt-6 rounded-cb border border-fd-border bg-fd-card p-5 sm:p-6">
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
            <dl className="flex flex-wrap gap-x-6 gap-y-3 text-sm">
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
                  className={`text-lg font-semibold tabular-nums ${t.failing ? 'text-cb-error-ink dark:text-cb-error' : ''}`}
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
              generated {progress.generated_at} · <code className="font-mono">{progress.commit}</code>
            </span>
          </div>
        </div>

        <div className="mt-4 flex flex-wrap gap-3">
          <Link
            href="/docs/progress"
            className="rounded-cb border border-fd-border px-4 py-2.5 text-sm font-medium transition-colors hover:bg-fd-accent sm:py-2"
          >
            Progress board
          </Link>
          <Link
            href="/docs/architecture"
            className="rounded-cb border border-fd-border px-4 py-2.5 text-sm font-medium transition-colors hover:bg-fd-accent sm:py-2"
          >
            Architecture
          </Link>
        </div>
      </section>
    </main>
  );
}
