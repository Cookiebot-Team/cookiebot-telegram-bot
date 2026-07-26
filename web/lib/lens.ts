// What the workbench is currently looking at.
//
// Two filters, applied together, because they answer two different questions
// and a tester needs both in the same session:
//
//   feature  "is the captcha correct?" — every scenario that touched one
//            feature, and all their traffic together. This is how validation
//            actually happens: not one test at a time, but one behaviour at a
//            time across every check that exercised it.
//   scenario "what did *this* check do?" — one span, drilled into.
//
// The scenario filter narrows the feature one. Picking a feature and then a
// scenario within it is the normal path; picking a scenario directly (from a
// failure, say) sets both, so the surrounding feature stays visible rather
// than the view snapping to one row with no context.
//
// Every pane — the timeline, the API-call log, the scenario picker — filters
// through `matchesLens` so they can never disagree about what is being shown,
// which for a validation tool is not a nicety: two panes silently applying
// different filters is a tool that lies.

import type { Feature, Scenario } from "@/types";

/** No filter on that axis. */
export const ALL_FEATURES = "all";
export const ALL_SCENARIOS = "all";
/** Traffic recorded while no scenario was active, and scenarios filed under
 * no feature. Its own bucket, deliberately: untagged work is work nobody can
 * roll up, and hiding it behind "all" is how it stays that way. */
export const UNTAGGED_SCENARIO = "untagged";
export const UNFILED_FEATURE = "unfiled";

export type FeatureLens = string;
export type ScenarioLens = string;

export interface Lens {
  feature: FeatureLens;
  scenario: ScenarioLens;
}

export const NO_LENS: Lens = { feature: ALL_FEATURES, scenario: ALL_SCENARIOS };

export function isFiltered(lens: Lens): boolean {
  return lens.feature !== ALL_FEATURES || lens.scenario !== ALL_SCENARIOS;
}

/** Which scenarios a feature selection admits. */
export function scenariosInFeature(scenarios: Scenario[], feature: FeatureLens): Scenario[] {
  if (feature === ALL_FEATURES) return scenarios;
  if (feature === UNFILED_FEATURE) return scenarios.filter((s) => s.feature === null);
  return scenarios.filter((s) => s.feature === feature);
}

/** Does a row tagged with `scenarioId` (or `null` for untagged) survive the
 * lens? The single predicate every pane uses — messages, API calls, and the
 * scenario picker's own list all go through it. */
export function matchesLens(
  scenarioId: string | null,
  lens: Lens,
  scenarios: Scenario[],
): boolean {
  if (lens.scenario === UNTAGGED_SCENARIO) return scenarioId === null;
  if (lens.scenario !== ALL_SCENARIOS) return scenarioId === lens.scenario;

  if (lens.feature === ALL_FEATURES) return true;
  // Untagged traffic belongs to no scenario, so it belongs to no feature
  // either — a feature filter must hide it rather than leak it into every
  // feature's view.
  if (scenarioId === null) return lens.feature === UNFILED_FEATURE;
  const scenario = scenarios.find((s) => s.id === scenarioId);
  if (!scenario) return false;
  if (lens.feature === UNFILED_FEATURE) return scenario.feature === null;
  return scenario.feature === lens.feature;
}

/** How a feature's run turned out, in one word plus the numbers behind it.
 *
 * Deliberately opinionated about precedence: any failure wins, then anything
 * still running, then "passed" only when at least one scenario passed and
 * nothing else is outstanding. A feature that nobody checked reads `untested`,
 * which is the row this whole view exists to make visible — a feature with no
 * scenarios looks identical to a passing one in every per-test report. */
export type FeatureVerdict = "failed" | "running" | "passed" | "mixed" | "untested";

export function featureVerdict(feature: Feature): FeatureVerdict {
  const counts = feature.status_counts;
  const total = feature.scenario_count;
  if (total === 0) return "untested";
  if ((counts.failed ?? 0) > 0) return "failed";
  if ((counts.running ?? 0) > 0) return "running";
  if ((counts.passed ?? 0) === total) return "passed";
  if ((counts.passed ?? 0) > 0) return "mixed";
  return "mixed";
}

export const VERDICT_STYLES: Record<FeatureVerdict, { label: string; className: string }> = {
  failed: { label: "failed", className: "bg-tg-red/20 text-tg-red" },
  running: { label: "running", className: "bg-tg-accent/20 text-tg-accent" },
  passed: { label: "passed", className: "bg-tg-green/20 text-tg-green" },
  mixed: { label: "mixed", className: "bg-tg-amber/20 text-tg-amber" },
  untested: { label: "untested", className: "bg-tg-muted/20 text-tg-muted" },
};

/** Failures first, then anything unfinished, then untested, then passed.
 *
 * Sorted for the reader, not alphabetically: the list is scanned top-down
 * looking for a problem, and a passing feature is the one thing that never
 * needs to be found. */
const VERDICT_ORDER: FeatureVerdict[] = ["failed", "mixed", "running", "untested", "passed"];

export function sortFeaturesForReview(features: Feature[]): Feature[] {
  return [...features].sort((a, b) => {
    const byVerdict =
      VERDICT_ORDER.indexOf(featureVerdict(a)) - VERDICT_ORDER.indexOf(featureVerdict(b));
    return byVerdict !== 0 ? byVerdict : a.title.localeCompare(b.title);
  });
}

/** Human name for a feature id, for a badge on a row. Falls back to the bare
 * id: a stale id must never crash the row it labels. */
export function featureLabel(features: Feature[], id: string | null): string {
  if (id === null) return "unfiled";
  return features.find((f) => f.id === id)?.title ?? id;
}

/** Same, for a scenario. */
export function scenarioLabel(scenarios: Scenario[], id: string | null): string {
  if (id === null) return "untagged";
  return scenarios.find((s) => s.id === id)?.name ?? id;
}
