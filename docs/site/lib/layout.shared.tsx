import type { BaseLayoutProps } from 'fumadocs-ui/layouts/shared';
import { appName, asset, gitConfig } from './shared';

/** The nav mark is the bot's actual avatar — the same picture a group sees in
 * its member list and on the web hub. A docs site for a bot people recognise
 * by its face should be recognisable the same way.
 *
 * The two halves swap order below `md`. On a phone the nav bar is the only
 * chrome there is, and the first thing under the reader's thumb should be the
 * word that says which site this is; the avatar follows it as a mark rather
 * than leading as an icon. On a laptop the picture leads, the way a favicon
 * does.
 *
 * A plain <img> rather than next/image: this is a static export, so there is
 * no optimiser to gain from, and `asset()` handles the Pages base path. */
export function baseOptions(): BaseLayoutProps {
  return {
    nav: {
      title: (
        <span className="inline-flex items-center gap-2">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src={asset('/brand/cookiebot-avatar.jpg')}
            alt=""
            width={28}
            height={28}
            className="order-2 size-7 rounded-full ring-1 ring-cb-gold-500/40 md:order-1"
          />
          <span className="order-1 font-display text-lg tracking-wide md:order-2">{appName}</span>
        </span>
      ),
    },
    githubUrl: `https://github.com/${gitConfig.user}/${gitConfig.repo}`,
  };
}
