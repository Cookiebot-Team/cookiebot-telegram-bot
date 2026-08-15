import { Chakra_Petch, Lobster, Space_Mono } from 'next/font/google';
import { Provider } from '@/components/provider';
import './global.css';

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
