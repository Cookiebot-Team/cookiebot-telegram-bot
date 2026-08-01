export const appName = 'Cookiebot';
export const docsRoute = '/docs';
export const docsImageRoute = '/og/docs';
export const docsContentRoute = '/llms.mdx/docs';

export const gitConfig = {
  user: 'Cookiebot-Team',
  repo: 'cookiebot-telegram-bot',
  branch: 'main',
};

/** Link to a file in the repository — used by feature pages to point at the
 * behaviour contract, the scenarios and the handler, which live in git rather
 * than in this site. */
export function repoFile(path: string): string {
  return `https://github.com/${gitConfig.user}/${gitConfig.repo}/blob/${gitConfig.branch}/${path}`;
}
