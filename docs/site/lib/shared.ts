export const appName = 'Cookiebot';
export const docsRoute = '/docs';
export const docsImageRoute = '/og/docs';
export const docsContentRoute = '/llms.mdx/docs';

export const gitConfig = {
  user: 'Cookiebot-Team',
  repo: 'cookiebot-telegram-bot',
  branch: 'main',
};

/** Prefix a file in `public/` with the deployment's base path.
 *
 * GitHub Pages serves this site from `/<repo>`, and a static export does not
 * rewrite `<img src>` the way it rewrites `<Link href>` — so a brand asset
 * referenced as `/brand/x.jpg` 404s in production and works locally, which is
 * the worst way for it to fail. Every asset URL goes through here. */
export function asset(path: string): string {
  return `${process.env.NEXT_PUBLIC_BASE_PATH ?? ''}${path}`;
}

/** Link to a file in the repository — used by feature pages to point at the
 * behaviour contract, the scenarios and the handler, which live in git rather
 * than in this site. */
export function repoFile(path: string): string {
  return `https://github.com/${gitConfig.user}/${gitConfig.repo}/blob/${gitConfig.branch}/${path}`;
}
