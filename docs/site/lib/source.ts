import { loader } from 'fumadocs-core/source';
import { docsContentRoute, docsImageRoute, docsRoute } from './shared';
import { defineDocs } from 'fumadocs-mdx/macro';
import { metaSchema, pageSchema } from 'fumadocs-core/source/schema';
import { z } from 'zod';

/** The status vocabulary is `scripts/spec.py`'s `Status`, not a second one
 * invented here. A page carrying a value outside it fails the build, which is
 * the point: the sidebar badge and the spec cannot drift into disagreeing. */
export const statusValues = ['done', 'partial', 'planned', 'blocked'] as const;
export type FeatureStatus = (typeof statusValues)[number];

/** Frontmatter every feature page carries, written by `cb.py docs-sync` from
 * `scripts/spec.py`. Hand-editing the body is expected; hand-editing these
 * fields is not — the sync overwrites them, and `cb.py docs-sync --check`
 * fails CI when they disagree with the spec.
 *
 * Scenario counts are deliberately absent: they change with every test run, so
 * they live in `content/progress.json` (which the drift check ignores) and
 * reach the page through `<FeatureHeader />`. */
const featureSchema = pageSchema.extend({
  status: z.enum(statusValues).optional(),
  milestone: z.string().optional(),
  area: z.string().optional(),
  layer: z.string().optional(),
  triggers: z.array(z.string()).optional(),
  v1_source: z.string().optional(),
  contract: z.string().optional(),
});

export type FeatureFrontmatter = z.infer<typeof featureSchema>;

const docs = defineDocs({
  dir: 'content/docs',
  docs: {
    schema: featureSchema,
    postprocess: {
      includeProcessedMarkdown: true,
    },
  },
  meta: {
    schema: metaSchema,
  },
});

// See https://fumadocs.dev/docs/headless/source-api for more info
export const source = loader({
  baseUrl: docsRoute,
  source: docs.toFumadocsSource(),
  plugins: [],
});

export function getPageImageUrl(page: (typeof source)['$inferPage']) {
  const segments = [...page.slugs, 'image.png'];

  return {
    segments,
    url: '/' + [page.locale, ...docsImageRoute.split('/'), ...segments].filter(Boolean).join('/'),
  };
}

export function getPageMarkdownUrl(page: (typeof source)['$inferPage']) {
  const segments = [...page.slugs, 'content.md'];

  return {
    segments,
    url: '/' + [page.locale, ...docsContentRoute.split('/'), ...segments].filter(Boolean).join('/'),
  };
}

export async function getLLMText(page: (typeof source)['$inferPage']) {
  const processed = await page.data.getText('processed');

  return `# ${page.data.title} (${page.url})

${processed}`;
}
