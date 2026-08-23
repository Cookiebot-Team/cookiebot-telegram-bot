import type { Viewport } from 'next';
import { Chakra_Petch, Lobster, Space_Mono } from 'next/font/google';
import { Provider } from '@/components/provider';
import './global.css';

/* `app/icon.jpg` and `app/apple-icon.jpg` are the bot's avatar: a tab, a
 * bookmark and an iOS home screen should show the same face a group sees in
 * its member list. `theme-color` paints the phone's own browser chrome in the
 * page's background — cream in light, cocoa in dark — so the site does not end
 * at a white bar it does not control. Both values are `--color-fd-background`
 * from `app/theme.css`; a media query cannot read a custom property here. */
export const viewport: Viewport = {
  themeColor: [
    { media: '(prefers-color-scheme: light)', color: '#fbf3e7' },
    { media: '(prefers-color-scheme: dark)', color: '#140e05' },
  ],
};

/* The web hub's three faces, in the same roles it gives them: Chakra Petch for
 * everything you read, Space Mono for anything you type, Lobster for the
 * wordmark alone. `app/theme.css` binds each to a --font-* token; components
 * name the token, never the family. */
const chakra = Chakra_Petch({
  subsets: ['latin'],
  weight: ['300', '400', '500', '600', '700'],
  variable: '--font-chakra',
});

const spaceMono = Space_Mono({
  subsets: ['latin'],
  weight: ['400', '700'],
  variable: '--font-space',
});

const lobster = Lobster({
  subsets: ['latin'],
  weight: ['400'],
  variable: '--font-lobster',
});

export default function Layout({ children }: LayoutProps<'/'>) {
  return (
    <html
      lang="en"
      className={`${chakra.variable} ${spaceMono.variable} ${lobster.variable} font-sans`}
      suppressHydrationWarning
    >
      <body className="flex flex-col min-h-screen">
        <Provider>{children}</Provider>
      </body>
    </html>
  );
}
