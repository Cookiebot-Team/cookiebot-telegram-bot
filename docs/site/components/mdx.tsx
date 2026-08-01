import defaultMdxComponents from 'fumadocs-ui/mdx';
import type { MDXComponents } from 'mdx/types';
import {
  ConsistencyFindings,
  DefectTable,
  FeatureHeader,
  FeatureTable,
  MilestoneProgress,
  ProgressOverview,
  ScenarioLedger,
  StatusBadge,
  StatusBar,
} from '@/components/progress';

/** The progress components are registered globally rather than imported per
 * page: every feature page uses `<FeatureHeader />`, and 50-odd identical
 * import lines are 50 chances for one page to drift. */
export function getMDXComponents(components?: MDXComponents) {
  return {
    ...defaultMdxComponents,
    ProgressOverview,
    MilestoneProgress,
    FeatureTable,
    FeatureHeader,
    ScenarioLedger,
    DefectTable,
    ConsistencyFindings,
    StatusBadge,
    StatusBar,
    ...components,
  } satisfies MDXComponents;
}

export const useMDXComponents = getMDXComponents;

declare global {
  type MDXProvidedComponents = ReturnType<typeof getMDXComponents>;
}
