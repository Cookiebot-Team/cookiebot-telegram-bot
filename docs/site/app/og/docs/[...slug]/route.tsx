import { getPageImageUrl, source } from '@/lib/source';
import { notFound } from 'next/navigation';
import { ImageResponse } from 'next/og';
import { appName } from '@/lib/shared';

export const revalidate = false;

/* A share card in the bot's own colours rather than Fumadocs' default black.
 *
 * These four values are the dark-theme tokens from `app/theme.css` written out
 * as literals — Satori renders in isolation and cannot read a CSS custom
 * property. Change them together with the tokens, or the link preview stops
 * matching the site it links to. */
const COCOA = '#140e05';
const CARD = '#1c1307';
const CREAM = '#f2e4d0';
const GOLD = '#dda531';
const BLUSH = '#ef86bb';

export async function GET(_req: Request, { params }: RouteContext<'/og/docs/[...slug]'>) {
  const { slug } = await params;
  const page = source.getPage(slug.slice(0, -1));
  if (!page) notFound();

  return new ImageResponse(
    (
      <div
        style={{
          display: 'flex',
          flexDirection: 'column',
          justifyContent: 'space-between',
          width: '100%',
          height: '100%',
          padding: '4.5rem',
          color: CREAM,
          backgroundColor: COCOA,
          backgroundImage: `radial-gradient(1200px 500px at 10% -20%, ${CARD}, transparent)`,
          borderBottom: `18px solid ${GOLD}`,
        }}
      >
        <div style={{ display: 'flex', flexDirection: 'column' }}>
          <p
            style={{
              margin: 0,
              fontSize: '30px',
              letterSpacing: '0.3em',
              textTransform: 'uppercase',
              color: GOLD,
            }}
          >
            {appName}
          </p>
          <p style={{ margin: '28px 0 0', fontSize: '78px', fontWeight: 800, lineHeight: 1.05 }}>
            {page.data.title}
          </p>
          <p style={{ margin: '24px 0 0', fontSize: '40px', color: '#b29a74', lineHeight: 1.3 }}>
            {page.data.description}
          </p>
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: '18px', fontSize: '28px' }}>
          <div
            style={{
              width: '18px',
              height: '18px',
              borderRadius: '999px',
              backgroundColor: BLUSH,
            }}
          />
          <span style={{ color: '#b29a74' }}>The Telegram group bot for furry communities</span>
        </div>
      </div>
    ),
    {
      width: 1200,
      height: 630,
    },
  );
}

export function generateStaticParams() {
  return source.getPages().map((page) => ({
    lang: page.locale,
    slug: getPageImageUrl(page).segments,
  }));
}
