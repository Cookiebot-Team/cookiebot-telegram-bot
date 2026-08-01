import { createMDX } from 'fumadocs-mdx/next';

const withMDX = createMDX();

// GitHub Pages serves a project site from /<repo>, not from the domain root, so
// every asset and link needs that prefix — but only there. `DOCS_BASE_PATH` is
// set by .github/workflows/docs.yml and unset locally, which keeps
// `bun run dev` on plain http://localhost:3002/.
const basePath = process.env.DOCS_BASE_PATH ?? '';

/** @type {import('next').NextConfig} */
const config = {
  output: 'export',
  reactStrictMode: true,
  basePath,
  // A static export has no image optimiser behind it.
  images: { unoptimized: true },
  // Pages has no rewrite layer, so `/docs/x` must be a real `/docs/x/index.html`.
  trailingSlash: true,
  env: { NEXT_PUBLIC_BASE_PATH: basePath },
};

export default withMDX(config);
